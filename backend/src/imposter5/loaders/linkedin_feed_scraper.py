"""LinkedIn feed scraper.

Uses :class:`loaders.linkedin_browser.LinkedInBrowserSession` to open a
stealth Chromium page, navigate to the LinkedIn feed, and extract the first
10 visible posts / activity cards.

Returned post dicts have the following *best-effort* keys::

    {
        "actor_name": str,
        "actor_url": str,
        "actor_headline": str,
        "post_text": str,
        "post_url": str | None,
        "media_type": str,   # "none" | "image" | "video" | "article"
        "scraped_at": str,   # ISO-8601 UTC
    }

All extraction is best-effort; missing values default to empty strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import logging
import random
import re
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from imposter5.automation_connector.behavior_policy import (
    behavior_summary,
    planned_scroll_delta,
    planned_scroll_passes,
    planned_wait_ms,
)
from imposter5.automation_connector.interaction_primitives import (
    maybe_expand_comments,
    move_pointer,
    scroll_page,
    wait_human,
)
from imposter5.automation_connector.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)

_FEED_URL = "https://www.linkedin.com/feed/"
_MAX_POSTS = 25
_MAX_SCROLL_PASSES = 4
_SCROLL_DELTA_Y = 900
_SCROLL_PAUSE_MS = 1_500
_SCROLL_DELTA_JITTER = 0.18
_SCROLL_PAUSE_JITTER = 0.35


def _log_user_id(user_id: str) -> str:
    return sha256(str(user_id or "").encode()).hexdigest()[:12]


@dataclass(frozen=True)
class LinkedInExtractionRules:
    """Centralized selectors and text markers that drift when site HTML changes."""

    primary_text_selectors: tuple[str, ...] = (
        'section[aria-label="Primary content"]',
        '[aria-label="Primary content"]',
        "main",
        "body",
    )
    feed_container_selectors: tuple[str, ...] = (
        "div.feed-shared-update-v2",
        "li.occludable-update",
        "div[data-urn]",
    )
    actor_name_selector: str = (
        ".update-components-actor__name, "
        ".feed-shared-actor__name, "
        "span.artdeco-entity-lockup__title"
    )
    actor_link_selector: str = (
        ".update-components-actor__meta a, "
        ".feed-shared-actor__container a, "
        "a.update-components-actor__image"
    )
    actor_headline_selector: str = (
        ".update-components-actor__description, "
        ".feed-shared-actor__description"
    )
    post_text_selector: str = (
        ".feed-shared-update-v2__description, "
        ".update-components-text, "
        ".feed-shared-text"
    )
    permalink_selector: str = "a[href*='/feed/update/'], a[href*='/posts/']"
    image_selector: str = ".feed-shared-image__container, .update-components-image"
    video_selector: str = ".feed-shared-linkedin-video, .update-components-video"
    article_selector: str = ".feed-shared-article, .update-components-article"
    passive_root_selector: str = "main *"
    passive_block_prefix: str = "Feed post "
    max_links_per_block: int = 80
    social_action_phrases: tuple[str, ...] = (
        "finds this insightful",
        "likes this",
        "loves this",
        "celebrates this",
        "supports this",
    )
    # Selectors for human-like variation actions (profile peeks, notifications, comment expands, hovers).
    # Keep centralized here so LinkedIn HTML drift repairs stay local.
    actor_link_selector: str = (
        ".update-components-actor__meta a, "
        ".feed-shared-actor__container a, "
        "a.update-components-actor__image"
    )
    nav_notifications_selector: str = (
        "a[href*='/notifications/'], "
        "button[aria-label*='Notifications'], "
        "[data-test-global-nav-link*='notifications']"
    )
    feed_nav_selector: str = (
        "a[href*='/feed/'][aria-label*='Home'], "
        "a[href*='/feed'], "
        "button[aria-label*='Home']"
    )
    comment_expand_selectors: tuple[str, ...] = (
        "button[aria-label*='comment' i]",
        ".comments-post-meta__show-comments",
        "button:has-text('Show more comments')",
        "span.artdeco-button__text:has-text('Comment')",
    )
    post_container_selectors: tuple[str, ...] = (
        "div.feed-shared-update-v2",
        "li.occludable-update",
        "div[data-urn]",
    )
    post_action_markers: tuple[str, ...] = (
        " Like Comment Repost Send",
        " Like Comment Share Send",
        " Like Comment",
    )


_RULES = LinkedInExtractionRules()


def _jittered_int(base: int, ratio: float) -> int:
    if base <= 0 or ratio <= 0:
        return base
    low = max(1, round(base * (1 - ratio)))
    high = max(low, round(base * (1 + ratio)))
    return random.randint(low, high)


def _scroll_pause_ms() -> int:
    return _jittered_int(_SCROLL_PAUSE_MS, _SCROLL_PAUSE_JITTER)


def _scroll_delta_y() -> int:
    return _jittered_int(_SCROLL_DELTA_Y, _SCROLL_DELTA_JITTER)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(el: Any) -> str:
    try:
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def _attr(el: Any, attr: str) -> str:
    try:
        val = el.get_attribute(attr)
        return (val or "").strip()
    except Exception:
        return ""


def _collapse_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _absolute_linkedin_url(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        raw = f"https://www.linkedin.com{raw}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    path = re.sub(r"/+$", "", parsed.path or "")
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", "", ""))


def _is_linkedin_identity_url(value: str) -> bool:
    parsed = urlparse(_absolute_linkedin_url(value))
    path = parsed.path or ""
    return parsed.netloc.endswith("linkedin.com") and (
        path.startswith("/in/")
        or path.startswith("/company/")
        or path.startswith("/school/")
    )


def _canonical_linkedin_identity_url(value: str) -> str:
    normalized = _absolute_linkedin_url(value)
    parsed = urlparse(normalized)
    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) >= 2 and parts[0] in {"in", "company", "school"}:
        return urlunparse((parsed.scheme, parsed.netloc, f"/{parts[0]}/{parts[1]}", "", "", ""))
    return normalized


def _identity_type_from_url(value: str) -> str:
    parsed = urlparse(_canonical_linkedin_identity_url(value))
    path = parsed.path or ""
    if path.startswith("/company/"):
        return "company"
    if path.startswith("/school/"):
        return "account"
    return "person"


def _is_linkedin_post_url(value: str) -> bool:
    parsed = urlparse(_absolute_linkedin_url(value))
    path = parsed.path or ""
    return parsed.netloc.endswith("linkedin.com") and (
        path.startswith("/feed/update/")
        or path.startswith("/posts/")
    )


def _display_name_key(value: str) -> str:
    text = _collapse_text(value)
    text = re.sub(r"\s+•\s+\d+(?:st|nd|rd|th)?$", "", text)
    text = re.sub(r"\b(?:Verified|Author)\b.*$", "", text).strip()
    text = re.sub(r"\s+\d[\d,]*\s+followers.*$", "", text).strip()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _degree_from_display_text(value: str) -> tuple[int | None, str]:
    match = re.search(r"\b(1st|2nd|3rd)\b", _collapse_text(value), flags=re.IGNORECASE)
    if match is None:
        return None, ""
    label = match.group(1).lower()
    return (1 if label == "1st" else 2 if label == "2nd" else 3), label


def _link_matches_name(link_text: str, name: str) -> bool:
    link_key = _display_name_key(link_text)
    name_key = _display_name_key(name)
    return bool(link_key and name_key and (link_key == name_key or link_key.startswith(name_key) or name_key.startswith(link_key)))


def _find_identity_link_for_name(links: list[dict], name: str) -> str:
    for link in links:
        href = _canonical_linkedin_identity_url(link.get("href"))
        if _is_linkedin_identity_url(href) and _link_matches_name(str(link.get("text") or ""), name):
            return href
    return ""


def _first_linkedin_post_url(links: list[dict]) -> str | None:
    for link in links:
        href = _absolute_linkedin_url(link.get("href"))
        if _is_linkedin_post_url(href):
            return href
    return None


def _identity_links(links: list[dict]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        href = _canonical_linkedin_identity_url(link.get("href"))
        text = _collapse_text(str(link.get("text") or ""))
        if not text or not _is_linkedin_identity_url(href):
            continue
        degree, degree_label = _degree_from_display_text(text)
        clean_name = re.sub(r"\s+•\s+\d+(?:st|nd|rd|th)?$", "", text).strip()
        key = (_display_name_key(clean_name), href)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        item = {"name": clean_name, "href": href, "entity_type": _identity_type_from_url(href)}
        avatar_url = _absolute_linkedin_url(str(link.get("avatar_url") or link.get("image_url") or ""))
        if avatar_url:
            item["avatar_url"] = avatar_url
        if degree is not None:
            item["degree"] = degree
            item["degree_label"] = degree_label
        result.append(item)
    return result


def _split_social_actor_phrase(value: str) -> tuple[str, list[dict[str, str]]]:
    text = _collapse_text(value)
    for action in _RULES.social_action_phrases:
        marker = f" {action} "
        if marker not in text:
            continue
        context_name, author_name = text.split(marker, 1)
        context_name = _collapse_text(context_name)
        author_name = _collapse_text(re.sub(r"\s+\d+\s*(?:s|m|h|d|w|mo|yr)s?$", "", author_name))
        if context_name and author_name:
            return author_name, [{"name": context_name, "relationship": action}]
    return text, []


def _parse_visible_count(value: str, label: str) -> int | None:
    lower = value.lower().replace(",", "")
    patterns = (
        rf"\b(\d+)\s+{label}s?\b",
        rf"\b{label}s?\s+(\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            return int(match.group(1))
    return None


def _extract_engagement_counts(text: str) -> dict[str, int]:
    return {
        "reactions": _parse_visible_count(text, "reaction") or _parse_visible_count(text, "like") or 0,
        "comments": _parse_visible_count(text, "comment") or 0,
        "reposts": _parse_visible_count(text, "repost") or 0,
    }


def _text_after_time_marker(value: str) -> tuple[str, str]:
    match = re.search(r"\b(\d+\s*(?:s|m|h|d|w|mo|yr)s?)\s*•\s*", value)
    if match is None:
        return "", value
    return match.group(1).strip(), value[match.end() :].strip()


def _trim_post_actions(value: str) -> str:
    for marker in _RULES.post_action_markers:
        idx = value.find(marker)
        if idx >= 0:
            value = value[:idx]
            break
    value = value.strip()
    if value.startswith("Follow "):
        value = value[len("Follow ") :].strip()
    return value


def _parse_feed_text_block(block: str, scraped_at: str) -> dict | None:
    text = _collapse_text(block)
    if text.startswith(_RULES.passive_block_prefix):
        text = text[len(_RULES.passive_block_prefix) :].strip()
    if not text:
        return None

    actor_segment, sep, remainder = text.partition(" • ")
    if not sep:
        return None
    actor_display_name = re.sub(
        r"\s+\d+\s*(?:s|m|h|d|w|mo|yr)s?$",
        "",
        actor_segment.strip(),
    )
    actor_name, related_people = _split_social_actor_phrase(actor_display_name)
    if not actor_name:
        return None

    relative_time, after_time = _text_after_time_marker(remainder)
    post_text = _trim_post_actions(after_time)
    if not post_text:
        return None

    headline = remainder
    if relative_time:
        marker = f"{relative_time} •"
        headline = remainder.split(marker, 1)[0].strip()

    return {
        "actor_name": actor_name,
        "actor_display_name": actor_display_name,
        "actor_url": "",
        "actor_headline": headline,
        "post_text": post_text,
        "post_url": None,
        "activity_type": "feed post",
        "relative_time": relative_time,
        "related_people": related_people,
        "engagement_counts": _extract_engagement_counts(text),
        "media_type": "none",
        "scraped_at": scraped_at,
    }


def _extract_posts_from_text(page: Any, scraped_at: str) -> list[dict]:
    """Fallback parser for LinkedIn's obfuscated feed DOM."""
    text = ""
    for selector in _RULES.primary_text_selectors:
        try:
            text = _collapse_text(page.inner_text(selector) or "")
        except Exception:
            text = ""
        if _RULES.passive_block_prefix in text:
            break

    if _RULES.passive_block_prefix not in text:
        return []

    posts: list[dict] = []
    for raw_block in re.split(r"(?=Feed post\s+)", text):
        if not raw_block.startswith(_RULES.passive_block_prefix):
            continue
        post = _parse_feed_text_block(raw_block, scraped_at)
        if post:
            posts.append(post)
        if len(posts) >= _MAX_POSTS:
            break
    if posts:
        logger.info("[linkedin_feed_scraper] extracted %d posts from text fallback", len(posts))
    return posts


def _augment_post_with_links(post: dict, links: list[dict]) -> dict:
    actor_url = _find_identity_link_for_name(links, str(post.get("actor_name") or ""))
    post_url = _first_linkedin_post_url(links)
    related_people = list(post.get("related_people") or [])
    identity_links = _identity_links(links)
    actor_url_key = _absolute_linkedin_url(actor_url)
    related_keys = {
        (_display_name_key(str(person.get("name") or "")), _absolute_linkedin_url(str(person.get("linkedin_url") or "")))
        for person in related_people
        if isinstance(person, dict)
    }

    for person in related_people:
        if not isinstance(person, dict):
            continue
        if person.get("linkedin_url"):
            continue
        link = _find_identity_link_for_name(links, str(person.get("name") or ""))
        if link:
            person["linkedin_url"] = link
            for identity_link in identity_links:
                if identity_link["href"] == link:
                    person["entity_type"] = person.get("entity_type") or identity_link.get("entity_type", "")
                    if identity_link.get("avatar_url") and not person.get("avatar_url"):
                        person["avatar_url"] = identity_link["avatar_url"]
                    if identity_link.get("degree") is not None:
                        person["degree"] = identity_link["degree"]
                        person["degree_label"] = identity_link.get("degree_label", "")
                    break
            related_keys.add((_display_name_key(str(person.get("name") or "")), _absolute_linkedin_url(link)))

    for link in identity_links:
        name = link["name"]
        href = link["href"]
        key = (_display_name_key(name), href)
        if href == actor_url_key or key in related_keys:
            continue
        if _link_matches_name(name, str(post.get("actor_name") or "")):
            continue
        related_people.append(
            {
                "name": name,
                "relationship": "mentioned",
                "linkedin_url": href,
                "entity_type": link.get("entity_type", ""),
                **({"avatar_url": link["avatar_url"]} if link.get("avatar_url") else {}),
                **({"degree": link["degree"], "degree_label": link.get("degree_label", "")} if link.get("degree") is not None else {}),
            }
        )
        related_keys.add(key)

    return {
        **post,
        "actor_url": actor_url or post.get("actor_url") or "",
        "post_url": post_url or post.get("post_url"),
        "related_people": related_people,
    }


def _extract_posts_from_feed_blocks(page: Any, scraped_at: str) -> list[dict]:
    """Parse rendered feed blocks and preserve already-present anchor URLs."""
    try:
        blocks = page.evaluate(
            r"""
            (config) => {
              const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
              const imageFor = (anchor) => {
                const direct = anchor.querySelector('img');
                const nearby = anchor.closest('div,span,li')?.querySelector('img');
                const img = direct || nearby;
                return img ? (img.currentSrc || img.src || img.getAttribute('src') || '') : '';
              };
              const hasFeedChild = (el) => Array.from(el.children || []).some(
                (child) => clean(child.innerText).startsWith(config.blockPrefix)
              );
              return Array.from(document.querySelectorAll(config.rootSelector))
                .filter((el) => clean(el.innerText).startsWith(config.blockPrefix) && !hasFeedChild(el))
                .slice(0, config.maxPosts)
                .map((el) => ({
                  text: clean(el.innerText),
                  links: Array.from(el.querySelectorAll('a[href]')).slice(0, config.maxLinksPerBlock).map((a) => ({
                    text: clean(a.innerText || a.textContent),
                    href: a.href || a.getAttribute('href') || '',
                    avatar_url: imageFor(a)
                  }))
                }));
            }
            """,
            {
                "rootSelector": _RULES.passive_root_selector,
                "blockPrefix": _RULES.passive_block_prefix,
                "maxPosts": _MAX_POSTS,
                "maxLinksPerBlock": _RULES.max_links_per_block,
            },
        )
    except Exception:
        return []

    posts: list[dict] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        post = _parse_feed_text_block(str(block.get("text") or ""), scraped_at)
        if not post:
            continue
        raw_links = block.get("links") if isinstance(block.get("links"), list) else []
        links = [link for link in raw_links if isinstance(link, dict)]
        posts.append(_augment_post_with_links(post, links))
    if posts:
        logger.info("[linkedin_feed_scraper] extracted %d posts from passive feed blocks", len(posts))
    return posts


def _container_links(container: Any) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    try:
        link_els = container.query_selector_all("a[href]") or []
    except Exception:
        return links
    for link_el in link_els[: _RULES.max_links_per_block]:
        try:
            img_el = link_el.query_selector("img")
        except Exception:
            img_el = None
        links.append(
            {
                "text": _text(link_el),
                "href": _attr(link_el, "href"),
                "avatar_url": _attr(img_el, "src") if img_el else "",
            }
        )
    return links


def _parse_container(container: Any, scraped_at: str) -> dict | None:
    """Parse a single feed post container element into a dict."""
    actor_el = container.query_selector(_RULES.actor_name_selector)
    actor_display_name = _text(actor_el) if actor_el else ""
    actor_name, related_people = _split_social_actor_phrase(actor_display_name)
    if not actor_name:
        return None  # skip skeleton / ad placeholders

    actor_link_el = container.query_selector(_RULES.actor_link_selector)
    actor_url = _absolute_linkedin_url(_attr(actor_link_el, "href") if actor_link_el else "")

    actor_headline_el = container.query_selector(_RULES.actor_headline_selector)
    actor_headline = _text(actor_headline_el) if actor_headline_el else ""

    text_el = container.query_selector(_RULES.post_text_selector)
    post_text = _text(text_el) if text_el else ""

    permalink_el = container.query_selector(_RULES.permalink_selector)
    post_url = _absolute_linkedin_url(_attr(permalink_el, "href") if permalink_el else "") or None
    activity_urn = _attr(container, "data-urn") or _attr(container, "data-id")
    container_text = _collapse_text(_text(container))

    media_type = "none"
    if container.query_selector(_RULES.image_selector):
        media_type = "image"
    elif container.query_selector(_RULES.video_selector):
        media_type = "video"
    elif container.query_selector(_RULES.article_selector):
        media_type = "article"

    post = {
        "actor_name": actor_name,
        "actor_display_name": actor_display_name,
        "actor_url": actor_url,
        "actor_headline": actor_headline,
        "post_text": post_text,
        "post_url": post_url,
        "activity_type": "feed post",
        "activity_urn": activity_urn,
        "related_people": related_people,
        "engagement_counts": _extract_engagement_counts(container_text),
        "media_type": media_type,
        "scraped_at": scraped_at,
    }
    return _augment_post_with_links(post, _container_links(container))


def _extract_posts_with_report(page: Any) -> tuple[list[dict], dict[str, Any]]:
    """Extract feed posts and report which bounded strategy succeeded."""
    posts: list[dict] = []
    attempts: list[dict[str, Any]] = []
    containers: list[Any] = []
    scraped_at = _now_iso()

    started = time.monotonic()
    for sel in _RULES.feed_container_selectors:
        containers = page.query_selector_all(sel) or []
        if containers:
            break
    for container in containers[:_MAX_POSTS]:
        try:
            post = _parse_container(container, scraped_at)
            if post:
                posts.append(post)
        except Exception as exc:
            logger.debug("[linkedin_feed_scraper] skipping container: %s", exc)
    attempts.append(
        {
            "strategy": "structured_containers",
            "matched_nodes": len(containers),
            "posts": len(posts),
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    )

    if not posts:
        started = time.monotonic()
        posts = _extract_posts_from_feed_blocks(page, scraped_at)
        attempts.append(
            {
                "strategy": "passive_feed_blocks",
                "matched_nodes": len(posts),
                "posts": len(posts),
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        )

    if not posts:
        started = time.monotonic()
        posts = _extract_posts_from_text(page, scraped_at)
        attempts.append(
            {
                "strategy": "text_fallback",
                "matched_nodes": 1 if posts else 0,
                "posts": len(posts),
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        )

    strategy = next((attempt["strategy"] for attempt in attempts if attempt["posts"]), "none")
    logger.info("[linkedin_feed_scraper] extracted %d posts", len(posts))
    return posts, {
        "strategy": strategy,
        "attempts": attempts,
        "agent_model_calls": 0,
    }


def _attach_extraction_meta(
    posts: list[dict],
    report: dict[str, Any],
    *,
    page_loads: int,
    scroll_passes: int,
    wheel_scrolls: int,
    wait_ms: list[int] | None = None,
    scroll_deltas: list[int] | None = None,
    permalink_backfill: bool = False,
    behavior_plan: dict[str, Any] | None = None,
    variation_actions: list[str] | None = None,
    session_recording: dict[str, Any] | None = None,
) -> list[dict]:
    behavior = behavior_summary(behavior_plan)
    meta = {
        **report,
        "page_loads": page_loads,
        "scroll_passes": scroll_passes,
        "wheel_scrolls": wheel_scrolls,
        "wait_ms": wait_ms or [],
        "scroll_deltas": scroll_deltas or [],
        "pacing_jitter": True,
        "permalink_backfill": permalink_backfill,
        "browser_actions": page_loads + wheel_scrolls,
        "variation_actions": variation_actions or [],
        "sides_performed": len(variation_actions or []),
    }
    if behavior:
        meta["behavior_policy"] = behavior
    if session_recording:
        meta["session_recording"] = session_recording
    return [{**post, "extraction_meta": meta} for post in posts]


def _extract_posts(page: Any) -> list[dict]:
    """Extract up to *_MAX_POSTS* feed posts from the current page DOM."""
    posts, _report = _extract_posts_with_report(page)
    return posts


def _post_identity(post: dict) -> str:
    """Return a stable key for de-duping posts across scroll passes."""
    explicit = post.get("post_url") or post.get("activity_urn")
    if explicit:
        return str(explicit)
    return "|".join(
        _collapse_text(str(post.get(key) or ""))
        for key in ("actor_name", "relative_time", "post_text")
    )


def _merge_unique_posts(existing: list[dict], incoming: list[dict]) -> list[dict]:
    seen = {_post_identity(post) for post in existing}
    for post in incoming:
        key = _post_identity(post)
        if not key or key in seen:
            continue
        seen.add(key)
        existing.append(post)
        if len(existing) >= _MAX_POSTS:
            break
    return existing


def _jittered(base: int, spread: int = 20) -> int:
    """Small random jitter helper (no plan needed)."""
    return random.randint(-spread, spread) + base


def _seeded_chance(chance: float, plan: dict[str, Any] | None, namespace: str) -> bool:
    """Deterministic per-run chance using the plan's run_id (same as primitives)."""
    if not plan:
        return random.random() < float(chance or 0)
    run_id = str(plan.get("run_id") or "")
    rng = random.Random(f"{run_id}:{namespace}")
    return rng.random() < float(chance or 0)


def _simulate_feed_reading_behaviors(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    variations: dict[str, Any],
    chances: dict[str, Any],
    actions_log: list[str],
) -> None:
    """Inject mouse moves, hovers over posts, and comment expands on the current feed view.
    Creates the 'reading' micro-variations (hover, look, expand, small scrolls) between main scroll passes.
    """
    try:
        # Hover a few visible post containers (mouse over content, like eyes scanning).
        containers = page.query_selector_all(_RULES.post_container_selectors[0]) or []
        hover_ch = chances.get("hover_read", 0.5 if variations.get("hover_and_read") else 0.2)
        for c in containers[:4]:
            if random.random() > hover_ch:
                continue
            try:
                b = c.bounding_box()
                if b:
                    mx = b["x"] + b["width"] * random.uniform(0.3, 0.7)
                    my = b["y"] + b["height"] * random.uniform(0.15, 0.6)
                    move_pointer(page, mx, my, plan, recorder=recorder)
                    wait_human(page, plan, 0, random.randint(120, 450), recorder=recorder)
            except Exception:
                continue
        actions_log.append("feed_hover_read")

        # Expand comments if enabled (uses the shared primitive + linkedin selectors).
        if variations.get("expand_comments"):
            expand_ch = chances.get("comment_expand", 0.1)
            # The primitive internally decides based on its hover/expand_chance from plan,
            # but we bias by calling only with probability.
            if random.random() < expand_ch:
                sels = getattr(_RULES, "comment_expand_selectors", ("button[aria-label*='comment' i]",))
                res = maybe_expand_comments(page, sels, plan, recorder=recorder)
                if res.get("expanded"):
                    actions_log.append("comment_expand")
    except Exception:
        pass


def _visit_notifications_variation(
    page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None, actions_log: list[str]
) -> bool:
    """Click notifications nav (mouse positioned), do a little scroll/reading there, return to feed."""
    try:
        from imposter5.automation_connector.interaction_primitives import click_element, update_status_ticker
        icon = page.locator(_RULES.nav_notifications_selector).first
        if not icon:
            return False
        update_status_ticker(page, "🔔 NOTIFICATIONS CHECK", "Navigating to Notifications tab...")
        click_element(page, icon, plan, recorder=recorder)
        wait_human(page, plan, 0, 600, recorder=recorder)
        # Small varied scroll + mouse in the notifs area.
        for i in range(2):
            scroll_page(page, plan, i, 280 + random.randint(-40, 60), recorder=recorder)
            wait_human(page, plan, i, 200, recorder=recorder)
            move_pointer(page, 300 + random.uniform(-10, 10), 300 + i * 80 + random.uniform(-15, 15), plan, recorder=recorder)
        actions_log.append("notifications_check")
        # Return to feed (prefer nav link, fallback to url).
        try:
            feed = page.locator(_RULES.feed_nav_selector).first
            if feed:
                update_status_ticker(page, "🧭 NAVIGATING", "Returning to Feed...")
                click_element(page, feed, plan, recorder=recorder)
            else:
                page.goto(_FEED_URL, wait_until="domcontentloaded")
        except Exception:
            page.goto(_FEED_URL, wait_until="domcontentloaded")
        wait_human(page, plan, 0, 500, recorder=recorder)
        return True
    except Exception:
        # Best effort; if nav fails just continue the feed run.
        return False


def _peek_random_profile_variation(
    page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None, actions_log: list[str]
) -> bool:
    """From a visible post, move realistically to an actor name/picture, click (styled via primitives if possible),
    on the profile do a few human scrolls + mouse moves over the work/experience area ("scroll down to their work history"),
    then back. Bounded and rare so the main feed extraction goal is preserved.
    """
    try:
        from imposter5.automation_connector.interaction_primitives import click_element, update_status_ticker
        containers = page.query_selector_all(_RULES.post_container_selectors[0]) or []
        if not containers:
            return False
        c = random.choice(containers[:5])
        link = c.query_selector(_RULES.actor_link_selector)
        if not link:
            return False
        
        update_status_ticker(page, "👤 PROFILE PEEK", "Peeking actor profile...")
        click_element(page, link, plan, recorder=recorder)
        wait_human(page, plan, 0, 700, recorder=recorder)

        # "scroll down to their work history" + reading moves (like a person would).
        for j in range(random.randint(1, 3)):
            d = 450 + random.randint(-80, 120)
            scroll_page(page, plan, j, d, recorder=recorder)
            wait_human(page, plan, j, random.randint(250, 650), recorder=recorder)
            # Wiggle mouse over the main profile content area (work history region).
            move_pointer(page, 420 + random.uniform(-30, 30), 520 + j * 70 + random.uniform(-25, 25), plan, recorder=recorder)
            # occasional small up like re-reading
            if random.random() < 0.35:
                scroll_page(page, plan, j, -90, recorder=recorder)
                wait_human(page, plan, j, 180, recorder=recorder)

        actions_log.append("profile_peek")
        # Back to feed (human backtrack).
        update_status_ticker(page, "🧭 BACKTRACKING", "Returning to Feed...")
        page.go_back(wait_until="domcontentloaded")
        wait_human(page, plan, 0, 550, recorder=recorder)
        return True
    except Exception:
        return False


def scrape_feed(
    user_id: str,
    *,
    raise_on_error: bool = False,
    behavior_plan: dict[str, Any] | None = None,
    headless: bool = True,
    visible: bool = False,
    record_video_dir: str | None = None,
) -> list[dict]:
    """Open a CloakBrowser session and return up to 10 LinkedIn feed posts.

    The run now performs a human-like varied session (mouse-positioned + eye-like
    bidirectional scrolls via the shared primitives, post hovers + comment expands,
    occasional bounded profile peeks that click a name/picture, scroll the work
    history area, then back, and notifications tab checks). These are selected
    according to the behavior_plan's persona, completion level, and optional
    "variation_guide" (custom variations) supplied on the target. This gives the
    LinkedIn observation path rich mouse scroll events and the micro-variations
    for a good digital twin of a human, while remaining the cost-optimized static
    path (no full natural-language prompt interpretation or generic goal runner).

    Parameters
    ----------
    user_id:
        Canonical user identifier; cookies are loaded/saved from S3 for this key.

    Returns:
    -------
    list[dict]
        Up to 10 post dicts.  Returns [] if not authenticated or on any error
        unless ``raise_on_error`` is true.
    """
    from imposter5.loaders.linkedin_browser import LinkedInBrowserSession, is_logged_in

    user_hash = _log_user_id(user_id)
    try:
        with LinkedInBrowserSession(
            user_id=user_id, headless=headless, record_video_dir=record_video_dir
        ) as page:
            if visible:
                try:
                    # Enable console logging for debugging
                    page.on("console", lambda msg: print(f"[BROWSER CONSOLE] {msg.text}", flush=True))
                    page.on("pageerror", lambda exc: print(f"[BROWSER EXCEPTION] {exc}", flush=True))

                    from imposter5.automation_connector.interaction_primitives import enable_visible_mouse_tracking
                    enable_visible_mouse_tracking(page)
                    try:
                        page.bring_to_front()
                    except Exception:
                        pass
                except Exception:
                    pass  # synthetic cursor is only for human visual judgment; never break the run
            logger.info("[linkedin_feed_scraper] navigating to feed for user_hash %s", user_hash)
            
            from imposter5.automation_connector.interaction_primitives import update_status_ticker
            update_status_ticker(page, "🧭 NAVIGATING", "Opening LinkedIn Feed...")
            page.goto(_FEED_URL, wait_until="domcontentloaded")

            if not is_logged_in(page):
                logger.warning(
                    "[linkedin_feed_scraper] user_hash %s is not authenticated — pausing for manual login",
                    user_hash,
                )
                print("[linkedin_feed_scraper] >>> NOT LOGGED IN. Pausing automation to allow manual login.")
                print("[linkedin_feed_scraper] >>> Please log in to LinkedIn in the browser window now.")
                
                # Wait up to 90 seconds for manual login
                logged_in = False
                for i in range(90):
                    page.wait_for_timeout(1000)
                    if is_logged_in(page):
                        logged_in = True
                        print("[linkedin_feed_scraper] >>> Manual login detected! Proceeding with simulation...")
                        break
                    if i % 10 == 0:
                        print(f"[linkedin_feed_scraper] >>> Still waiting for manual login... ({90 - i} seconds remaining)")
                
                if not logged_in:
                    if raise_on_error:
                        raise RuntimeError(
                            "LinkedIn browser session is not authenticated; manual login timed out (90 seconds)."
                        )
                    return []

            # Recorder + plan-driven variations (mouse-positioned scrolls, reading hovers, comment expands,
            # occasional profile peeks to work history, notifications checks, bidirectional/eye-like scrolls).
            # This makes the LinkedIn static observation path use the same high-quality human twin mechanics
            # (from behavior_policy + primitives) as the generic/agent paths, without adopting full prompt
            # interpretation or goal_runner (per the "everything but the prompt action stuff" boundary).
            recorder = SessionRecorder(behavior_plan)
            posts: list[dict] = []
            latest_report: dict[str, Any] = {"strategy": "none", "attempts": [], "agent_model_calls": 0}
            scroll_passes = 0
            wheel_scrolls = 0  # counts scroll_page invocations
            wait_ms: list[int] = []
            scroll_deltas: list[int] = []
            variation_actions: list[str] = []
            behavior_active = bool(behavior_summary(behavior_plan))
            max_scroll_passes = planned_scroll_passes(behavior_plan, _MAX_SCROLL_PASSES)
            variations = (behavior_plan or {}).get("variations") or {}
            chances = (behavior_plan or {}).get("variation_chances") or {}
            max_sides = int(variations.get("max_side_actions", 2 if behavior_active else 0))
            sides_done = 0

            # Initial settle with mouse move for realistic entry (mouse scroll / reading position).
            try:
                wait_human(page, behavior_plan, 0, 900, recorder=recorder)
                # Rough move into feed area (will be refined by scroll_page etc).
                move_pointer(page, _jittered(380, 30), _jittered(420, 40), behavior_plan, recorder=recorder)
            except Exception:
                pass

            for pass_index in range(max_scroll_passes):
                scroll_passes = pass_index + 1
                pause_fallback = _SCROLL_PAUSE_MS if behavior_active else _scroll_pause_ms()
                pause_ms = planned_wait_ms(behavior_plan, pass_index, pause_fallback)
                wait_ms.append(pause_ms)
                page.wait_for_timeout(pause_ms)
                new_posts, latest_report = _extract_posts_with_report(page)
                posts = _merge_unique_posts(posts, new_posts)

                # Human reading behaviors on the current view: mouse moves + hovers over posts + expands.
                _simulate_feed_reading_behaviors(page, behavior_plan, recorder, variations, chances, variation_actions)

                if len(posts) >= _MAX_POSTS:
                    break
                if pass_index == max_scroll_passes - 1:
                    break

                # Optional side variations (profile peek, notifications) before next scroll.
                if sides_done < max_sides:
                    did_side = False
                    if variations.get("notifications_check") and _seeded_chance(chances.get("notifications", 0.12), behavior_plan, f"notif:{pass_index}"):
                        if _visit_notifications_variation(page, behavior_plan, recorder, variation_actions):
                            sides_done += 1
                            did_side = True
                            # Re-extract after returning to feed (may have new visible posts from scroll state).
                            new_posts, _r = _extract_posts_with_report(page)
                            posts = _merge_unique_posts(posts, new_posts)
                    if not did_side and variations.get("profile_peeks") and _seeded_chance(chances.get("profile_peek", 0.18), behavior_plan, f"peek:{pass_index}"):
                        if _peek_random_profile_variation(page, behavior_plan, recorder, variation_actions):
                            sides_done += 1
                            new_posts, _r = _extract_posts_with_report(page)
                            posts = _merge_unique_posts(posts, new_posts)

                delta_fallback = _SCROLL_DELTA_Y if behavior_active else _scroll_delta_y()
                delta_y = planned_scroll_delta(behavior_plan, pass_index, delta_fallback)

                # Bidirectional / eye-like: sometimes small up or mixed before/after the main delta.
                if variations.get("bidirectional_scroll") and _seeded_chance(chances.get("bidir_scroll", 0.25), behavior_plan, f"bidir:{pass_index}"):
                    up_delta = -abs(delta_y) // 3 or -120
                    scroll_page(page, behavior_plan, pass_index, up_delta, recorder=recorder)
                    wait_human(page, behavior_plan, pass_index, 180, recorder=recorder)
                    wheel_scrolls += 1
                    # small corrective down or hover
                    scroll_page(page, behavior_plan, pass_index, abs(delta_y) // 4 or 90, recorder=recorder)
                    wheel_scrolls += 1

                scroll_deltas.append(delta_y)
                # Use the enhanced scroll_page (positions mouse over content for "mouse scroll event",
                # then wheel). This + the reading behaviors above give the varied mouse trajectories.
                used = scroll_page(page, behavior_plan, pass_index, delta_y, recorder=recorder)
                scroll_deltas[-1] = used  # in case plan adjusted
                wheel_scrolls += 1
                logger.info(
                    "[linkedin_feed_scraper] scroll pass %d/%d collected %d posts (sides:%d)",
                    pass_index + 1,
                    max_scroll_passes,
                    len(posts),
                    sides_done,
                )
            posts = posts[:_MAX_POSTS]
            if not posts and raise_on_error:
                raise RuntimeError("LinkedIn feed loaded but no parseable posts were found.")
            return _attach_extraction_meta(
                posts,
                latest_report,
                page_loads=1,
                scroll_passes=scroll_passes,
                wheel_scrolls=wheel_scrolls,
                wait_ms=wait_ms,
                scroll_deltas=scroll_deltas,
                behavior_plan=behavior_plan,
                variation_actions=variation_actions,
                session_recording=recorder.payload() if recorder else None,
            )
    except Exception as exc:
        logger.error("[linkedin_feed_scraper] failed for user_hash %s: %s", user_hash, exc)
        if raise_on_error:
            raise RuntimeError(f"LinkedIn feed scrape failed: {exc}") from exc
        return []


def scrape_post_identity(user_id: str, post_url: str, *, raise_on_error: bool = False, headless: bool = True) -> dict | None:
    """Open one LinkedIn post permalink and return its visible identity metadata."""
    from imposter5.loaders.linkedin_browser import LinkedInBrowserSession, is_logged_in

    if not _is_linkedin_post_url(post_url):
        return None

    user_hash = _log_user_id(user_id)
    try:
        with LinkedInBrowserSession(user_id=user_id, headless=headless) as page:
            logger.info("[linkedin_feed_scraper] navigating to post permalink for user_hash %s", user_hash)
            page.goto(_absolute_linkedin_url(post_url), wait_until="domcontentloaded")
            if not is_logged_in(page):
                logger.warning(
                    "[linkedin_feed_scraper] user_hash %s is not authenticated for post backfill",
                    user_hash,
                )
                if raise_on_error:
                    raise RuntimeError(
                        "LinkedIn browser session is not authenticated; saved cookies are missing or expired."
                    )
                return None

            pause_ms = _scroll_pause_ms()
            page.wait_for_timeout(pause_ms)
            posts, report = _extract_posts_with_report(page)
            if not posts:
                if raise_on_error:
                    raise RuntimeError("LinkedIn post loaded but no parseable post was found.")
                return None

            requested_url = _absolute_linkedin_url(post_url)
            annotated = _attach_extraction_meta(
                posts,
                report,
                page_loads=1,
                scroll_passes=1,
                wheel_scrolls=0,
                wait_ms=[pause_ms],
                scroll_deltas=[],
                permalink_backfill=True,
            )
            for post in annotated:
                if _absolute_linkedin_url(post.get("post_url")) == requested_url:
                    return post
            return annotated[0]
    except Exception as exc:
        logger.error("[linkedin_feed_scraper] post identity scrape failed for user_hash %s: %s", user_hash, exc)
        if raise_on_error:
            raise RuntimeError(f"LinkedIn post identity scrape failed: {exc}") from exc
        return None
