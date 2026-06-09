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
    planned_scroll_passes,
)
from imposter5.automation_connector.interaction_primitives import (
    maybe_expand_comments,
    move_pointer,
    perceive_after_render,
    scroll_page,
    wait_human,
)
from imposter5.automation_connector.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)

_FEED_URL = "https://www.linkedin.com/feed/"
_NOTIFICATIONS_URL = "https://www.linkedin.com/notifications/"
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
    opened_interest: list[dict] | None = None,
    interest_terms: list[str] | None = None,
    markov_failures: int = 0,
    video_start_offset_ms: int | None = None,
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
        "behavior_driver": "markov_goal_hybrid",
        "interest_terms": interest_terms or [],
        "opened_interest": opened_interest or [],
        "interest_opens": len(opened_interest or []),
        "markov_failures": markov_failures,
        "video_start_offset_ms": video_start_offset_ms,
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


# Resilient candidates for a "notification row" (LinkedIn rotates hashed
# classes, so prefer role/href anchors that survive cosmetic DOM churn).
# Verified against the live notifications page (DOM discovery 2026-06): each of
# these yields ~24 rows / ~10 visible with real text. ``main article`` is the
# cleanest, the data-attr / nt-card are resilient fallbacks. The old
# role='listitem' / li / componentkey selectors matched 0 rows here.
_NOTIFICATION_ITEM_SELECTORS: tuple[str, ...] = (
    "main article",
    "main [data-finite-scroll-hotkey-item]",
    "main .nt-card",
)


def _visible_targets(page: Any, selectors: tuple[str, ...], limit: int) -> list[tuple[Any, dict]]:
    """Return up to ``limit`` (element, bounding_box) pairs for REAL, on-screen
    elements matching the first selector that yields any. Used so the cursor
    only ever moves to something a human could actually be looking at — never to
    arbitrary fixed coordinates (which reads as aimless, robotic drift)."""
    for sel in selectors:
        out: list[tuple[Any, dict]] = []
        try:
            els = page.query_selector_all(sel) or []
        except Exception:
            continue
        for el in els:
            try:
                b = el.bounding_box()
            except Exception:
                b = None
            # Must be a sensible, visible, in-viewport block to be a real target.
            if not b or b["width"] < 60 or b["height"] < 24:
                continue
            if b["y"] < 40 or b["y"] > 1200:
                continue
            out.append((el, b))
            if len(out) >= limit:
                break
        if out:
            return out
    return []


def _move_to_target(
    page: Any, box: dict, plan: dict[str, Any] | None, recorder: SessionRecorder | None
) -> tuple[float, float]:
    """Purposeful move to a real element's interior (slight natural offset).

    Returns the cursor's realized landing point so callers can click exactly
    where the hand ended up instead of letting Playwright recenter to the
    element middle (which would erase the human endpoint imprecision)."""
    cx = box["x"] + box["width"] * random.uniform(0.2, 0.5)
    cy = box["y"] + box["height"] * random.uniform(0.35, 0.6)
    meta = move_pointer(page, cx, cy, plan, recorder=recorder)
    ex = float((meta or {}).get("x", cx))
    ey = float((meta or {}).get("y", cy))
    return ex, ey


def _bounded_nav(
    page: Any,
    selectors: tuple[str, ...],
    fallback_url: str,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    *,
    label: str,
    detail: str,
) -> bool:
    """Navigate via a purposeful, SHORT-timeout click on the first resolvable
    real nav link, falling back to a direct goto. Never blocks on a
    non-actionable match (the old path clicked ``.first`` of dozens of matches
    and could hang ~25s waiting out the default actionability timeout)."""
    from imposter5.automation_connector.interaction_primitives import update_status_ticker

    update_status_ticker(page, label, detail)
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            box = loc.bounding_box(timeout=1200)  # fast-fail; do not wait out 20s
            if not box:
                continue
            ex, ey = _move_to_target(page, box, plan, recorder)
            wait_human(page, plan, 0, random.randint(160, 340), recorder=recorder)
            # Click where the cursor actually landed (no center re-snap).
            page.mouse.click(ex, ey)
            return True
        except Exception:
            continue
    try:
        page.goto(fallback_url, wait_until="domcontentloaded")
        return True
    except Exception:
        return False


def _return_to_feed(page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None) -> None:
    """Return to the feed reliably and a little slowly (purposeful Home click)."""
    _bounded_nav(
        page,
        ("a[href='/feed/']", "a[href*='/feed/'][aria-label*='Home' i]"),
        _FEED_URL,
        plan,
        recorder,
        label="🧭 NAVIGATING",
        detail="Returning to Feed...",
    )


# Stable action phrasings LinkedIn renders inside every notification row. These
# play the same role the "Feed post N" ARIA prefix plays for the feed scraper's
# text_fallback: they survive class/structure churn because they are the
# human-readable content, letting us recover items when row selectors drift.
_NOTIFICATION_VERB_MARKERS: tuple[str, ...] = (
    "commented on",
    "reacted to",
    "liked your",
    "likes your",
    "mentioned you",
    "started following",
    "viewed your profile",
    "celebrates",
    "endorsed",
    "sent you",
    "invited you",
    "accepted your",
    "your post",
    "shared a post",
)


def _notification_text_fallback(page: Any, limit: int) -> list[str]:
    """Layer-3 capture: parse the notifications column's rendered innerText into
    items by stable action phrasing.

    Mirrors the feed scraper's ``_extract_posts_from_text`` fallback so that a
    total drift of the row selectors (``main article`` etc.) still recovers
    items instead of returning zero — the same resilience that keeps feed
    capture working through LinkedIn DOM churn."""
    try:
        blob = _collapse_text(page.inner_text("main") or "")
    except Exception:
        return []
    if not blob:
        return []
    pattern = "|".join(re.escape(v) for v in _NOTIFICATION_VERB_MARKERS)
    items: list[str] = []
    for m in re.finditer(pattern, blob, flags=re.IGNORECASE):
        # Include the actor name immediately preceding the verb + a little after.
        start = max(0, m.start() - 48)
        chunk = _collapse_text(blob[start : m.start() + 110]).strip()
        if not chunk:
            continue
        if any(chunk in seen or seen in chunk for seen in items):
            continue
        items.append(chunk[:160])
        if len(items) >= limit:
            break
    return items


def _visit_notifications_variation(
    page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None, actions_log: list[str]
) -> bool:
    """Check notifications like a human: open the tab, read the top couple of
    items (cursor moves only to the real rows), then come back to the feed.

    Hard time-budgeted so it can never sit on the notifications page, and the
    cursor never drifts to meaningless coordinates."""
    try:
        # Open notifications via a bounded, purposeful click (move to the real
        # nav link, short-timeout click) with a direct-nav fallback — never the
        # old ~25s actionability hang on a hidden duplicate `.first` match.
        _bounded_nav(
            page,
            ("a[href*='/notifications/']", "button[aria-label*='Notifications' i]"),
            _NOTIFICATIONS_URL,
            plan,
            recorder,
            label="🔔 NOTIFICATIONS CHECK",
            detail="Navigating to Notifications tab...",
        )
        # Notifications view just rendered: perceive it before acting.
        perceive_after_render(page, plan, recorder=recorder)
        # Let the notification list paint before reading. The human-like nav-icon
        # click changes the URL via the SPA router, but LinkedIn's client-side
        # route to notifications frequently leaves the list UN-hydrated (measured:
        # 0 rows for 7s+), whereas a full document load renders the rows at once.
        # So: poll briefly for rows; if none appear, force a full goto reload
        # (invisible in a movie beyond a flicker) which reliably renders content.
        def _rows_present() -> bool:
            return bool(_visible_targets(page, _NOTIFICATION_ITEM_SELECTORS, limit=1))

        settle_deadline = time.time() + 1.5
        while time.time() < settle_deadline and not _rows_present():
            page.wait_for_timeout(300)
        if not _rows_present():
            try:
                page.goto(_NOTIFICATIONS_URL, wait_until="domcontentloaded")
                perceive_after_render(page, plan, recorder=recorder)
            except Exception:
                pass
            reload_deadline = time.time() + 4.0
            while time.time() < reload_deadline and not _rows_present():
                page.wait_for_timeout(400)

        deadline = time.time() + 9.0  # never sit on notifications longer than this
        captured: list[str] = []

        # Read the top couple of real notification rows (purposeful moves only).
        for el, box in _visible_targets(page, _NOTIFICATION_ITEM_SELECTORS, limit=2):
            if time.time() > deadline:
                break
            _move_to_target(page, box, plan, recorder)
            wait_human(page, plan, 0, random.randint(300, 700), recorder=recorder)
            txt = _collapse_text(_text(el))[:160]
            if txt and txt not in captured:
                captured.append(txt)

        # One small scroll to bring the next row into view, then read it too.
        if time.time() < deadline:
            scroll_page(page, plan, 0, 260 + random.randint(-40, 60), recorder=recorder)
            wait_human(page, plan, 0, random.randint(250, 500), recorder=recorder)
            for el, box in _visible_targets(page, _NOTIFICATION_ITEM_SELECTORS, limit=1):
                _move_to_target(page, box, plan, recorder)
                wait_human(page, plan, 0, random.randint(250, 500), recorder=recorder)
                txt = _collapse_text(_text(el))[:160]
                if txt and txt not in captured:
                    captured.append(txt)

        # Layer-3 resilience: if the structured rows yielded nothing (selector
        # drift), recover items from the rendered text the same way the feed
        # scraper's text_fallback does.
        if not captured:
            captured = _notification_text_fallback(page, limit=3)

        actions_log.append("notifications_check")
        if captured:
            actions_log.append(f"notifications_read:{len(captured)}")
            if recorder is not None:
                try:
                    recorder.record("notifications_read", metadata={"count": len(captured), "items": captured})
                except Exception:
                    pass

        # Come back to the feed (reliably, a little slowly), then perceive it.
        _return_to_feed(page, plan, recorder)
        perceive_after_render(page, plan, recorder=recorder)
        return True
    except Exception:
        # Best effort: make sure we don't strand the run on notifications.
        try:
            _return_to_feed(page, plan, recorder)
        except Exception:
            pass
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
        # Profile view just rendered: perceive it before scrolling their history.
        perceive_after_render(page, plan, recorder=recorder)

        # "scroll down to their work history" + reading moves (like a person would).
        profile_content_selectors = ("main section", "main article", "main [role='listitem']")
        for j in range(random.randint(1, 3)):
            d = 450 + random.randint(-80, 120)
            scroll_page(page, plan, j, d, recorder=recorder)
            wait_human(page, plan, j, random.randint(250, 650), recorder=recorder)
            # Purposeful: move over a real content block (work/experience region),
            # never to a fixed/arbitrary point (that reads as aimless drift).
            for el, box in _visible_targets(page, profile_content_selectors, limit=1):
                _move_to_target(page, box, plan, recorder)
            # occasional small up like re-reading
            if random.random() < 0.35:
                scroll_page(page, plan, j, -90, recorder=recorder)
                wait_human(page, plan, j, 180, recorder=recorder)

        actions_log.append("profile_peek")
        # Back to feed (human backtrack).
        update_status_ticker(page, "🧭 BACKTRACKING", "Returning to Feed...")
        page.go_back(wait_until="domcontentloaded")
        # Back-navigation re-renders the prior feed view: perceive before acting.
        perceive_after_render(page, plan, recorder=recorder)
        return True
    except Exception:
        # A peek can navigate to a profile and then fail mid-scroll; never strand
        # the run off-feed (the next extract would read the wrong DOM).
        if not _on_feed(page):
            try:
                _return_to_feed(page, plan, recorder)
                perceive_after_render(page, plan, recorder=recorder)
            except Exception:
                logger.debug("[linkedin_feed_scraper] profile-peek recovery failed", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# Goal + Markov hybrid: Markov drives the scan motion, the goal owns the clicks
# --------------------------------------------------------------------------- #
#
# The feed run is a hybrid: a semi-Markov walk DRIVES the ambient scan (scroll /
# hover / dwell down the feed) so the low-level motion differs every run and
# looks like a person reading, while the GOAL layer (extract evidence, open a
# genuinely interesting post) sits on top. Click/typing are intentionally absent
# from the scan matrix below: on a live feed the only meaningful click is "open
# that post I care about", and a human does that on purpose — not as a random
# walk step. So the random walk never fires an arbitrary navigation; the goal
# layer owns the single intentional click.
FEED_SCAN_MATRIX: dict[str, dict[str, float]] = {
    "idle":        {"idle": 0.15, "mousemove": 0.25, "scroll_down": 0.45, "scroll_up": 0.05, "hover": 0.10},
    "mousemove":   {"idle": 0.15, "mousemove": 0.15, "scroll_down": 0.45, "scroll_up": 0.05, "hover": 0.20},
    "scroll_down": {"idle": 0.25, "mousemove": 0.15, "scroll_down": 0.45, "scroll_up": 0.05, "hover": 0.10},
    "scroll_up":   {"idle": 0.20, "mousemove": 0.20, "scroll_down": 0.40, "scroll_up": 0.10, "hover": 0.10},
    "hover":       {"idle": 0.25, "mousemove": 0.20, "scroll_down": 0.35, "scroll_up": 0.05, "hover": 0.15},
}

# Generic "worth stopping for" markers for a professional feed. When the run
# carries explicit ICP/interest terms (from the plan or its variations) those
# dominate; otherwise these catch role/opportunity-shaped posts. With no terms
# at all we fall back to post SUBSTANCE (longer, meatier posts catch the eye) —
# never a fabricated relevance signal.
_DEFAULT_INTEREST_TERMS: tuple[str, ...] = (
    "hiring", "we're hiring", "join our team", "open role", "new role",
    "excited to announce", "looking for", "opportunity", "now hiring",
)

# Where the Markov scan is allowed to aim its hovers: feed posts and the people
# inside them — never global nav chrome, Like/Comment controls, or sidebar ads.
_FEED_HOVER_SELECTORS: tuple[str, ...] = (
    "main a[href*='/feed/update/']",
    "main a[href*='/in/']",
)


def _scroll_y(page: Any) -> int | None:
    """Current vertical scroll offset, or None if it can't be read."""
    try:
        return int(page.evaluate("window.scrollY") or 0)
    except Exception:
        return None


def _run_feed_ambient(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    *,
    steps: int,
    state: dict[str, Any] | None = None,
    ensure_scroll: bool = True,
) -> dict[str, Any]:
    """Drive a short semi-Markov burst that scans the feed (Markov owns the
    scroll/hover/dwell motion). Uses the click/typing-free ``FEED_SCAN_MATRIX``
    so the goal layer stays the only thing that opens a post.

    ``state`` threads the prior burst's walk forward (state/intent/sojourn) so
    chained bursts read as one continuous scan instead of a repeating
    "settle -> idle -> reading" preamble every few seconds. The returned dict is
    fed back in as ``state`` next burst. When ``ensure_scroll`` is set and the
    burst didn't actually advance the feed (a swallowed failure, or an
    idle/hover-heavy walk), one deterministic human scroll is issued so evidence
    gathering never silently stalls."""
    cont = dict(state or {})
    if steps <= 0:
        return cont
    from imposter5.loaders.markov_simulator import run_markov_simulation

    burst_plan = dict(plan or {})
    burst_plan["markov_matrix"] = FEED_SCAN_MATRIX
    y_before = _scroll_y(page)
    result: dict[str, Any] = {}
    failed = False
    try:
        result = run_markov_simulation(
            page,
            burst_plan,
            recorder=recorder,
            max_steps=steps,
            initial_state=cont.get("final_state"),
            initial_intent=cont.get("final_intent"),
            intent_steps_left=cont.get("intent_steps_left"),
            suppress_intro_wait=bool(cont),
            mousemove_targets=_RULES.post_container_selectors,
            hover_targets=_FEED_HOVER_SELECTORS,
        )
    except Exception:
        logger.debug("[linkedin_feed_scraper] feed ambient markov burst failed", exc_info=True)
        failed = True

    if ensure_scroll:
        y_after = _scroll_y(page)
        advanced = (
            y_before is not None and y_after is not None and (y_after - y_before) >= 250
        )
        if failed or not advanced:
            try:
                scroll_page(
                    page,
                    plan,
                    pass_index=0,
                    fallback_delta_y=_jittered(720, 140),
                    recorder=recorder,
                )
            except Exception:
                logger.debug("[linkedin_feed_scraper] scroll fallback failed", exc_info=True)

    result["markov_failed"] = failed
    return result


def _resolve_interest_terms(plan: dict[str, Any] | None) -> list[str]:
    """Collect the run's ICP / interest terms from the plan (and its variations).

    Falls back to a generic professional-interest vocabulary when nothing is
    configured, so the human-interest behavior is alive on a default run rather
    than depending solely on post length."""
    terms: list[str] = []
    if isinstance(plan, dict):
        variations = plan.get("variations") if isinstance(plan.get("variations"), dict) else {}
        target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
        sources = [
            plan.get("interest_terms"), plan.get("icp_terms"),
            variations.get("interest_terms"), variations.get("icp_terms"),
            target.get("interest_terms"), target.get("icp_terms"),
        ]
        for src in sources:
            if isinstance(src, str):
                terms.extend(t.strip() for t in src.split(",") if t.strip())
            elif isinstance(src, (list, tuple)):
                terms.extend(str(t).strip() for t in src if str(t).strip())
    if not terms:
        terms = list(_DEFAULT_INTEREST_TERMS)
    return [t.lower() for t in terms]


def _score_post_interest(text: str, terms: list[str]) -> float:
    """Score how much a human would want to stop on this post.

    Explicit interest/ICP term hits dominate; post substance is a mild secondary
    signal so that, absent terms, the meatiest visible post still wins."""
    if not text:
        return 0.0
    low = text.lower()
    score = 0.0
    for term in terms:
        if term and term in low:
            score += 2.0
    score += min(1.5, len(text) / 1000.0)
    return score


def _collect_visible_containers(page: Any) -> list[Any]:
    """All post containers currently in/near the viewport, across every container
    selector (mirrors extraction's multi-selector fallback so interest scoring
    doesn't go blind when LinkedIn drops one class)."""
    seen_ids: set[str] = set()
    out: list[Any] = []
    for selector in _RULES.post_container_selectors:
        try:
            found = page.query_selector_all(selector) or []
        except Exception:
            continue
        for c in found[:20]:
            try:
                urn = c.get_attribute("data-urn") or c.get_attribute("data-id") or ""
            except Exception:
                urn = ""
            key = urn or f"{id(c)}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            out.append(c)
    return out


def _container_identity(container: Any, text: str) -> str:
    """Stable-ish identity for de-duping interest opens within a session."""
    try:
        link = container.query_selector(_RULES.permalink_selector)
        href = link.get_attribute("href") if link else None
        if href:
            return href.split("?")[0]
    except Exception:
        pass
    try:
        urn = container.get_attribute("data-urn")
        if urn:
            return urn
    except Exception:
        pass
    return _collapse_text(text)[:120]


def _open_interesting_post(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    interest_terms: list[str],
    actions_log: list[str],
    *,
    opened_identities: set[str] | None = None,
) -> dict | None:
    """The "oh, that one's interesting" behavior: scan the visible posts, and if
    one genuinely clears the bar (matches the run's ICP/interest terms, or is the
    most substantive in view), move to it, open it, read it (Markov-driven), then
    return to the feed. Returns a small descriptor, or None if nothing in view
    was worth stopping for."""
    from imposter5.automation_connector.interaction_primitives import click_element, update_status_ticker

    opened_identities = opened_identities if opened_identities is not None else set()
    scored: list[tuple[float, Any, dict, str, str]] = []
    for c in _collect_visible_containers(page):
        try:
            box = c.bounding_box()
        except Exception:
            box = None
        # Only react to posts actually in / near the viewport (a person responds
        # to what they can see, not off-screen DOM).
        if not box or box["height"] < 80 or box["y"] < -200 or box["y"] > 1200:
            continue
        try:
            txt = _collapse_text(_text(c))
        except Exception:
            txt = ""
        if not txt:
            continue
        identity = _container_identity(c, txt)
        if identity in opened_identities:
            continue  # don't re-open the same post we already read this session
        scored.append((_score_post_interest(txt, interest_terms), c, box, txt, identity))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, container, box, text, identity = scored[0]
    matched = [term for term in interest_terms if term and term in text.lower()]
    # Bar: a real ICP/interest term match dominates; otherwise only stop on a
    # clearly substantive post (never a fabricated relevance signal).
    bar = 2.0 if matched else 1.1
    if best_score < bar:
        return None

    update_status_ticker(page, "✨ INTERESTING POST", "Found a post worth reading; opening it...")
    _move_to_target(page, box, plan, recorder)
    wait_human(page, plan, 0, random.randint(220, 520), recorder=recorder)

    # Open via the post's own permalink (timestamp link) when present, else the
    # post text body — never the bare container, whose center can land on the
    # Like/Comment/Send action bar instead of opening the post.
    target = None
    actor_name = ""
    post_url = ""
    try:
        link = container.query_selector(_RULES.permalink_selector)
        if link:
            target = link
            post_url = (link.get_attribute("href") or "").split("?")[0]
    except Exception:
        target = None
    if target is None:
        try:
            target = container.query_selector(_RULES.post_text_selector)
        except Exception:
            target = None
    if target is None:
        target = container
    try:
        actor_el = container.query_selector(_RULES.actor_name_selector)
        actor_name = _collapse_text(_text(actor_el)) if actor_el else ""
    except Exception:
        actor_name = ""

    try:
        url_before = page.url
    except Exception:
        url_before = None
    try:
        click_element(page, target, plan, recorder=recorder)
    except Exception:
        return None
    opened_identities.add(identity)

    # The post view just rendered — perceive it before reading.
    perceive_after_render(page, plan, recorder=recorder)
    # Read it: a short Markov burst drives the in-post scroll/dwell.
    _run_feed_ambient(page, plan, recorder, steps=random.randint(3, 6))
    # A curious reader sometimes opens the comments.
    try:
        if random.random() < 0.5:
            sels = getattr(_RULES, "comment_expand_selectors", ("button[aria-label*='comment' i]",))
            maybe_expand_comments(page, sels, plan, recorder=recorder)
    except Exception:
        pass

    _ensure_back_on_feed(page, plan, recorder, url_before)

    snippet = text[:160]
    actions_log.append("interest_open")
    descriptor = {
        "matched_terms": matched,
        "score": round(best_score, 2),
        "snippet": snippet,
        "actor_name": actor_name,
        "post_url": post_url,
    }
    if recorder is not None:
        try:
            recorder.record("interest_open", metadata=descriptor)
        except Exception:
            pass
    return descriptor


def _on_feed(page: Any) -> bool:
    """True when the current view is the main feed (URL or feed container)."""
    try:
        if "/feed" in (page.url or ""):
            return True
    except Exception:
        pass
    for selector in _RULES.feed_container_selectors:
        try:
            if page.query_selector(selector) is not None:
                return True
        except Exception:
            continue
    return False


def _ensure_back_on_feed(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    url_before: str | None,
) -> None:
    """Guarantee we end up back on the feed after reading a post, whether opening
    it caused a full navigation OR a modal overlay. A single un-verified Escape
    was the prior strand bug (the run could sit on a post/modal and then extract
    the wrong DOM)."""
    from imposter5.automation_connector.interaction_primitives import update_status_ticker

    try:
        navigated = url_before is not None and page.url != url_before
    except Exception:
        navigated = False

    if navigated:
        update_status_ticker(page, "🧭 BACKTRACKING", "Returning to Feed...")
        try:
            page.go_back(wait_until="domcontentloaded")
        except Exception:
            pass
    else:
        # Dismiss a post/detail modal; retry once before falling back.
        for _ in range(2):
            try:
                page.keyboard.press("Escape")
            except Exception:
                break
            try:
                page.wait_for_timeout(random.randint(180, 360))
            except Exception:
                pass
            if _on_feed(page):
                break

    # Verified fallback: if we're still not on the feed, navigate home explicitly.
    if not _on_feed(page):
        try:
            _return_to_feed(page, plan, recorder)
        except Exception:
            logger.debug("[linkedin_feed_scraper] _return_to_feed fallback failed", exc_info=True)
    try:
        perceive_after_render(page, plan, recorder=recorder)
    except Exception:
        pass


def scrape_feed(
    user_id: str,
    *,
    raise_on_error: bool = False,
    behavior_plan: dict[str, Any] | None = None,
    headless: bool = True,
    visible: bool = False,
    record_video_dir: str | None = None,
    run_fp_agent: bool = False,
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
            # Video capture begins when the persistent context opens (here);
            # mark it now so we can align the event clock to the video clock.
            video_capture_start_monotonic = time.monotonic()
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
            video_offset_ms: int | None = None
            try:
                video_offset_ms = round(
                    (video_capture_start_monotonic - recorder.started_monotonic) * 1000
                )
            except Exception:
                video_offset_ms = None

            # Arm the FP-agent (mus.js) behavioral recorder on this authenticated
            # page so the canned LinkedIn gold run produces a real bot-likeness
            # verdict, not a null one.
            if run_fp_agent:
                try:
                    from imposter5.fp_agent.fp_agent_local_redteam_detector_test import ensure_mus_recording
                    ensure_mus_recording(page)
                except Exception:
                    logger.debug("[linkedin_feed_scraper] could not start mus recording", exc_info=True)

            posts: list[dict] = []
            latest_report: dict[str, Any] = {"strategy": "none", "attempts": [], "agent_model_calls": 0}
            variation_actions: list[str] = []
            opened_interest: list[dict] = []
            behavior_active = bool(behavior_summary(behavior_plan))
            max_bursts = planned_scroll_passes(behavior_plan, _MAX_SCROLL_PASSES)
            variations = (behavior_plan or {}).get("variations") or {}
            chances = (behavior_plan or {}).get("variation_chances") or {}
            max_sides = int(variations.get("max_side_actions", 2 if behavior_active else 0))
            sides_done = 0
            interest_terms = _resolve_interest_terms(behavior_plan)
            max_interest = int(variations.get("max_interest_opens", 2))
            interest_chance = float(chances.get("interest_open", 0.55 if interest_terms else 0.35))
            opened_count = 0
            opened_identities: set[str] = set()
            markov_steps_total = 0
            markov_failures = 0
            markov_state: dict[str, Any] = {}

            # The feed view just rendered: pay a floored human perceive-decide
            # latency before the first behavioral action (no instant reaction).
            try:
                perceive_after_render(page, behavior_plan, recorder=recorder)
            except Exception:
                pass

            # Initial settle + a rough move into the feed (entry reading position).
            try:
                wait_human(page, behavior_plan, 0, 900, recorder=recorder)
                move_pointer(page, _jittered(380, 30), _jittered(420, 40), behavior_plan, recorder=recorder)
            except Exception:
                pass

            for burst_index in range(max_bursts):
                # GOAL: gather evidence from whatever is currently in view.
                new_posts, latest_report = _extract_posts_with_report(page)
                posts = _merge_unique_posts(posts, new_posts)

                # GOAL: human interest — if a genuinely compelling post is in view,
                # stop and read it (the "oh, that one's interesting" behavior).
                if opened_count < max_interest and _seeded_chance(
                    interest_chance, behavior_plan, f"interest:{burst_index}"
                ):
                    opened = _open_interesting_post(
                        page, behavior_plan, recorder, interest_terms, variation_actions,
                        opened_identities=opened_identities,
                    )
                    if opened:
                        opened_count += 1
                        opened_interest.append(opened)
                        new_posts, _r = _extract_posts_with_report(page)
                        posts = _merge_unique_posts(posts, new_posts)

                if len(posts) >= _MAX_POSTS:
                    break

                # Micro-reading: plan-driven hover-over-posts + comment expands on the
                # current view (honors bidirectional_scroll / hover_and_read /
                # expand_comments variation flags so they aren't inert on this path).
                if behavior_active and variations:
                    try:
                        variation_actions.extend(
                            run_feed_reading_variations(
                                page, behavior_plan, recorder,
                                variations=variations, chances=chances,
                            )
                        )
                    except Exception:
                        logger.debug("[linkedin_feed_scraper] micro-reading variation failed", exc_info=True)

                # Occasional bounded side trips (notifications / profile peek).
                if sides_done < max_sides:
                    did_side = False
                    if variations.get("notifications_check") and _seeded_chance(chances.get("notifications", 0.12), behavior_plan, f"notif:{burst_index}"):
                        if _visit_notifications_variation(page, behavior_plan, recorder, variation_actions):
                            sides_done += 1
                            did_side = True
                            new_posts, _r = _extract_posts_with_report(page)
                            posts = _merge_unique_posts(posts, new_posts)
                    if not did_side and variations.get("profile_peeks") and _seeded_chance(chances.get("profile_peek", 0.18), behavior_plan, f"peek:{burst_index}"):
                        if _peek_random_profile_variation(page, behavior_plan, recorder, variation_actions):
                            sides_done += 1
                            new_posts, _r = _extract_posts_with_report(page)
                            posts = _merge_unique_posts(posts, new_posts)

                # MARKOV drives the scan: a short semi-Markov burst scrolls / hovers
                # / dwells down the feed. This IS the scrolling — goal-free motion
                # that differs every run instead of a fixed scroll cadence. The walk
                # state threads forward so the whole session is one continuous scan.
                steps = random.randint(4, 7) if behavior_active else random.randint(3, 5)
                markov_state = _run_feed_ambient(
                    page, behavior_plan, recorder, steps=steps, state=markov_state
                )
                if markov_state.get("markov_failed"):
                    markov_failures += 1
                markov_steps_total += steps
                logger.info(
                    "[linkedin_feed_scraper] burst %d/%d collected %d posts (sides:%d, interest:%d)",
                    burst_index + 1, max_bursts, len(posts), sides_done, opened_count,
                )

            # Final harvest: capture whatever the last scan burst scrolled into view
            # (extraction runs at the TOP of each burst, so without this the posts
            # revealed by the final scroll would be dropped).
            final_posts, final_report = _extract_posts_with_report(page)
            posts = _merge_unique_posts(posts, final_posts)
            if final_report.get("attempts"):
                latest_report = final_report

            posts = posts[:_MAX_POSTS]
            if not posts and raise_on_error:
                raise RuntimeError("LinkedIn feed loaded but no parseable posts were found.")

            fp_frames = None
            if run_fp_agent:
                try:
                    from imposter5.fp_agent.fp_agent_local_redteam_detector_test import stop_and_get_mus_frames
                    fp_frames = stop_and_get_mus_frames(page)
                except Exception:
                    logger.debug("[linkedin_feed_scraper] could not stop mus recording", exc_info=True)

            annotated = _attach_extraction_meta(
                posts,
                latest_report,
                page_loads=1,
                scroll_passes=max_bursts,
                wheel_scrolls=markov_steps_total,
                wait_ms=[],
                scroll_deltas=[],
                behavior_plan=behavior_plan,
                variation_actions=variation_actions,
                session_recording=recorder.payload() if recorder else None,
                opened_interest=opened_interest,
                interest_terms=interest_terms,
                markov_failures=markov_failures,
                video_start_offset_ms=video_offset_ms,
            )
            # Carry the (potentially large) FP-agent frames on the FIRST post only
            # so the caller can compute a verdict without duplicating the payload
            # across every post's extraction_meta.
            if annotated and fp_frames is not None:
                first_meta = dict(annotated[0].get("extraction_meta") or {})
                first_meta["fp_frames"] = fp_frames
                annotated[0] = {**annotated[0], "extraction_meta": first_meta}
            return annotated
    except Exception as exc:
        logger.error("[linkedin_feed_scraper] failed for user_hash %s: %s", user_hash, exc)
        if raise_on_error:
            raise RuntimeError(f"LinkedIn feed scrape failed: {exc}") from exc
        return []


# --------------------------------------------------------------------------- #
# Public adapter surface
#
# Consumed by the prompt-driven goal runner so the *organic* LinkedIn path reuses
# the exact same structured extraction + human reading/side-trip mechanics as this
# canned scraper, instead of duplicating them (one source of truth for LinkedIn
# literacy). These are thin wrappers over the module-private helpers above.
# --------------------------------------------------------------------------- #
LINKEDIN_FEED_URL = _FEED_URL


def extract_visible_posts(page: Any) -> list[dict]:
    """Structured feed-post extraction for the current LinkedIn DOM (best-effort)."""
    return _extract_posts(page)


def merge_unique_posts(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Dedup-merge posts across scroll passes using stable identity keys."""
    return _merge_unique_posts(existing, incoming)


def run_feed_reading_variations(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    *,
    variations: dict[str, Any] | None = None,
    chances: dict[str, Any] | None = None,
) -> list[str]:
    """Hover-over-posts + comment-expand micro-reading behaviors on the current view."""
    actions: list[str] = []
    _simulate_feed_reading_behaviors(page, plan, recorder, variations or {}, chances or {}, actions)
    return actions


def visit_notifications(page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None) -> bool:
    """Click the notifications nav, read briefly, and return to the feed."""
    actions: list[str] = []
    return _visit_notifications_variation(page, plan, recorder, actions)


def peek_random_profile(page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None) -> bool:
    """Click a visible actor, scroll their work history, then back to the feed."""
    actions: list[str] = []
    return _peek_random_profile_variation(page, plan, recorder, actions)


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
