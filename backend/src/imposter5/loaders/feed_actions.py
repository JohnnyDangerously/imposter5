"""Feed-native human action primitives — the proven, Blue-validated behaviors that
make a feed-browsing session look human.

These are the *physical behaviors* only (scan a feed burst, capture what scrolled
past, peek at engagement, like a relevant post, check notifications, glance at a
secondary surface, search/open a profile, look a person up). They own NO session
arc and NO cross-session variety — that is the Story compiler/executor's job.

Affordances are resolved through a per-site Red Team Automation profile via the
``RoleResolver`` cascade (profile CSS -> semantic -> text/ARIA -> vision), so the
SAME behaviors run on the gauntlet, LinkedIn, or any conformant site without
hard-coding one site's element ids here. Every move/scroll/click/type/dwell still
goes through the shared humanized motor primitives, so the behavioral surface the
Blue detector sees is unchanged from the version that scored HUMAN_EVADED.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from imposter5.automation_connector.affordance import (
    AutomationProfile,
    RoleResolver,
    resolve_profile,
)
from imposter5.automation_connector.interaction_primitives import (
    click_element,
    hover_element,
    move_pointer,
    perceive_after_render,
    scroll_page,
    type_text,
    update_status_ticker,
    wait_human,
)
from imposter5.automation_connector.session_recorder import SessionRecorder
from imposter5.loaders.content_scorer import ContentScorer
from imposter5.loaders.linkedin_feed_scraper import FEED_SCAN_MATRIX
from imposter5.loaders.markov_simulator import run_markov_simulation

logger = logging.getLogger(__name__)

_DEFAULT_GAUNTLET_INTERESTS: tuple[str, ...] = (
    "data engineer",
    "ml platform",
    "analytics lead",
    "staff data engineer",
    "head of data",
)


def resolve_interest_terms(
    plan: dict[str, Any] | None, profile: AutomationProfile | None = None
) -> list[str]:
    """ICP / interest terms from the plan, then the profile campaign, else a default."""
    terms: list[str] = []
    if isinstance(plan, dict):
        variations = plan.get("variations") if isinstance(plan.get("variations"), dict) else {}
        target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
        for src in (
            plan.get("interest_terms"), plan.get("icp_terms"),
            variations.get("interest_terms"), variations.get("icp_terms"),
            target.get("interest_terms"), target.get("icp_terms"),
        ):
            if isinstance(src, str):
                terms.extend(t.strip() for t in src.split(",") if t.strip())
            elif isinstance(src, (list, tuple)):
                terms.extend(str(t).strip() for t in src if str(t).strip())
    if not terms and profile is not None:
        ct = profile.campaign.get("interest_terms")
        if isinstance(ct, (list, tuple)):
            terms.extend(str(t).strip() for t in ct if str(t).strip())
    if not terms:
        terms = list(_DEFAULT_GAUNTLET_INTERESTS)
    return terms


# =========================================================================== #
# Session state container
# =========================================================================== #
@dataclass
class FeedSession:
    """Per-session feed state threaded through the scan cycle + excursions."""

    page: Any
    plan: dict[str, Any] | None
    recorder: SessionRecorder | None
    resolver: RoleResolver
    interest_terms: list[str]
    scorer: ContentScorer
    captured_post_ids: set[str] = field(default_factory=set)
    captured_authors: list[str] = field(default_factory=list)
    pending_lookups: list[str] = field(default_factory=list)
    markov_state: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    start_monotonic: float = 0.0
    duration_s: float = 240.0

    @property
    def time_left(self) -> float:
        return self.duration_s - (time.monotonic() - self.start_monotonic)


def new_feed_session(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    *,
    duration_s: float = 240.0,
    interest_terms: list[str] | None = None,
    profile: AutomationProfile | None = None,
) -> FeedSession:
    if profile is None:
        url = None
        if isinstance(plan, dict):
            url = plan.get("url") or plan.get("start_url")
        if not url:
            try:
                url = page.url
            except Exception:
                url = None
        profile = resolve_profile(plan, url=url)
    resolver = RoleResolver(page, profile)
    persona = None
    if isinstance(plan, dict):
        persona = plan.get("persona_description") or plan.get("persona")
    summary: dict[str, Any] = {
        "feed_scan_bursts": 0, "markov_steps": 0, "posts_captured": 0, "peeks": 0,
        "notifications_visited": 0, "profiles_opened": 0, "searches": 0, "lookups": 0,
        "likes": 0, "glances": 0, "content_actions": 0, "profile": profile.name,
    }
    pending: list[str] = []
    if isinstance(plan, dict) and isinstance(plan.get("lookup_people"), (list, tuple)):
        pending = [str(p).strip() for p in plan["lookup_people"] if str(p).strip()]
    fs = FeedSession(
        page=page, plan=plan, recorder=recorder, resolver=resolver,
        interest_terms=interest_terms or resolve_interest_terms(plan, profile),
        scorer=ContentScorer(persona), pending_lookups=pending, summary=summary,
        start_monotonic=time.monotonic(), duration_s=duration_s,
    )
    summary["interest_terms"] = fs.interest_terms
    return fs


# --- viewport-scoped element helpers ---------------------------------------- #
def _vp_handles(fs: FeedSession, role: str, *, limit: int = 25) -> list[tuple[Any, dict]]:
    """Usable, in-viewport (locator, box) pairs for a role (best-effort)."""
    out: list[tuple[Any, dict]] = []
    for loc in fs.resolver.all(role, limit=limit * 3):
        try:
            box = loc.bounding_box()
        except Exception:
            box = None
        if not box or box["width"] < 30 or box["height"] < 16:
            continue
        if box["y"] < -120 or box["y"] > 1200:
            continue
        out.append((loc, box))
        if len(out) >= limit:
            break
    return out


def _within(post: Any, candidates: list[str]) -> Any | None:
    """First usable sub-element of ``post`` matching one of the candidate selectors."""
    for sub in candidates:
        try:
            t = post.locator(sub).first
            if t.count() > 0 and t.is_visible():
                return t
        except Exception:
            continue
    return None


# =========================================================================== #
# Low-level behaviors
# =========================================================================== #
def scan_feed_burst(fs: FeedSession, *, steps: int) -> None:
    """One semi-Markov ambient scan burst over the feed, aiming hovers/moves at the
    resolved feed-post container (never arbitrary viewport coordinates)."""
    burst_plan = dict(fs.plan or {})
    burst_plan["markov_matrix"] = FEED_SCAN_MATRIX
    scan_sel = fs.resolver.selector_for("feed_post") or "article"
    targets = (scan_sel,)
    state = fs.markov_state
    try:
        fs.markov_state = run_markov_simulation(
            fs.page, burst_plan, recorder=fs.recorder, max_steps=steps,
            initial_state=state.get("final_state"), initial_intent=state.get("final_intent"),
            intent_steps_left=state.get("intent_steps_left"), suppress_intro_wait=bool(state),
            mousemove_targets=targets, hover_targets=targets,
        )
    except Exception:
        logger.debug("[feed_actions] feed scan burst failed", exc_info=True)
        try:
            scroll_page(fs.page, fs.plan, pass_index=0, fallback_delta_y=720, recorder=fs.recorder)
        except Exception:
            pass


def capture_visible_posts(fs: FeedSession, sink: list[dict[str, Any]] | None = None) -> int:
    """Capture every feed post currently on screen — the actual data-gathering job.

    A pure DOM read (no input events), parameterized by the site profile's post +
    field selectors, so it works on any site without changing the behavioral surface.
    """
    post_sel = fs.resolver.selector_for("feed_post") or "article"
    sels = {
        "post": post_sel,
        "author": fs.resolver.field_selector("feed_author") or ".name",
        "headline": fs.resolver.field_selector("feed_headline") or ".meta",
        "text": fs.resolver.field_selector("feed_text") or ".text",
    }
    try:
        posts = fs.page.evaluate(
            """(sels) => {
                const vh = innerHeight || 800;
                const out = [];
                document.querySelectorAll(sels.post).forEach(p => {
                    const r = p.getBoundingClientRect();
                    if (r.top > vh - 40) return;
                    const pick = (s) => { try { const e = s && p.querySelector(s); return e ? (e.innerText || '').trim() : ''; } catch (_) { return ''; } };
                    const author = pick(sels.author), text = pick(sels.text);
                    const id = p.getAttribute('data-post-id') || p.getAttribute('data-urn')
                        || p.getAttribute('data-id') || ('h:' + (author + '|' + text).slice(0, 48));
                    out.push({ id: id, author: author, headline: pick(sels.headline), text: text.slice(0, 200) });
                });
                return out;
            }""",
            sels,
        )
    except Exception:
        return 0
    n = 0
    for p in posts or []:
        pid = p.get("id") if isinstance(p, dict) else None
        if not pid or pid in fs.captured_post_ids:
            continue
        fs.captured_post_ids.add(pid)
        n += 1
        author = (p.get("author") or "").strip() if isinstance(p, dict) else ""
        if author:
            fs.captured_authors.append(author)
        if sink is not None and isinstance(p, dict):
            sink.append(p)
        if fs.recorder is not None:
            try:
                fs.recorder.record("feed_capture", metadata={
                    "post_id": pid, "author": p.get("author", ""),
                    "headline": p.get("headline", ""), "snippet": (p.get("text") or "")[:120]})
            except Exception:
                pass
    return n


def peek_post_engagement(fs: FeedSession) -> bool:
    """Glance at a post's comments / reactions — the ambient 'looking at comments'
    micro-behavior that breaks up a pure scroll."""
    posts = _vp_handles(fs, "feed_post", limit=8)
    if not posts:
        return False
    post, _box = random.choice(posts)
    target = None
    for role in ("feed_actions_row", "feed_author", "feed_text"):
        target = _within(post, fs.resolver.field_candidates(role))
        if target is not None:
            break
    if target is None:
        return False
    try:
        update_status_ticker(fs.page, "💬 PEEKING", "Glancing at comments / reactions...")
        hover_element(fs.page, target, fs.plan, recorder=fs.recorder)
        wait_human(fs.page, fs.plan, 0, random.randint(220, 620), recorder=fs.recorder)
        if fs.recorder is not None:
            try:
                fs.recorder.record("post_peek", metadata={})
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("[feed_actions] post-engagement peek failed", exc_info=True)
        return False


def return_home(fs: FeedSession) -> None:
    el = fs.resolver.one("nav_home")
    if el is None:
        return
    try:
        click_element(fs.page, el, fs.plan, recorder=fs.recorder)
        perceive_after_render(fs.page, fs.plan, recorder=fs.recorder)
    except Exception:
        logger.debug("[feed_actions] return-home failed", exc_info=True)


def visit_notifications(fs: FeedSession) -> bool:
    """Open notifications, read down the list, then back to the feed."""
    el = fs.resolver.one("nav_notifications")
    if el is None:
        return False
    try:
        update_status_ticker(fs.page, "🔔 NOTIFICATIONS", "Checking notifications...")
        click_element(fs.page, el, fs.plan, recorder=fs.recorder)
        perceive_after_render(fs.page, fs.plan, recorder=fs.recorder)
        for i in range(random.randint(2, 4)):
            scroll_page(fs.page, fs.plan, pass_index=i, fallback_delta_y=random.randint(420, 700), recorder=fs.recorder)
            wait_human(fs.page, fs.plan, i, random.randint(280, 720), recorder=fs.recorder)
        if fs.recorder is not None:
            try:
                fs.recorder.record("notifications_visit", metadata={})
            except Exception:
                pass
        return_home(fs)
        return True
    except Exception:
        logger.debug("[feed_actions] notifications visit failed", exc_info=True)
        return_home(fs)
        return False


_GLANCE_ROLE = {"network": "nav_network", "jobs": "nav_jobs", "messages": "nav_messages"}


def glance(fs: FeedSession, surface: str) -> bool:
    """Quick human glance at a secondary nav surface (network/jobs/messages)."""
    role = _GLANCE_ROLE.get(surface)
    el = fs.resolver.one(role) if role else None
    if el is None:
        return False
    try:
        update_status_ticker(fs.page, "🧭 BROWSING", f"Glancing at {surface}...")
        click_element(fs.page, el, fs.plan, recorder=fs.recorder)
        perceive_after_render(fs.page, fs.plan, recorder=fs.recorder)
        for i in range(random.randint(1, 2)):
            scroll_page(fs.page, fs.plan, pass_index=i, fallback_delta_y=random.randint(360, 560), recorder=fs.recorder)
            wait_human(fs.page, fs.plan, i, random.randint(240, 560), recorder=fs.recorder)
        return_home(fs)
        return True
    except Exception:
        logger.debug("[feed_actions] glance at %s failed", surface, exc_info=True)
        return_home(fs)
        return False


def search_and_open_profile(fs: FeedSession, term: str, *, read_sections: bool = True) -> bool:
    """Search an interest term, scan results, open an interesting profile, read a
    few sections, then back home."""
    page, plan, recorder = fs.page, fs.plan, fs.recorder
    si = fs.resolver.one("search_input")
    if si is None:
        return False
    try:
        update_status_ticker(page, "🔎 SEARCHING", f"Searching for '{term}'...")
        click_element(page, si, plan, recorder=recorder)
        wait_human(page, plan, 0, random.randint(160, 380), recorder=recorder)
        type_text(page, si, term, plan, recorder=recorder)
        wait_human(page, plan, 0, random.randint(180, 420), recorder=recorder)
        sg = fs.resolver.one("search_submit")
        if sg is not None:
            click_element(page, sg, plan, recorder=recorder)
        else:
            try:
                si.press("Enter")
            except Exception:
                pass
        perceive_after_render(page, plan, recorder=recorder)

        for i in range(random.randint(2, 4)):
            scroll_page(page, plan, pass_index=i, fallback_delta_y=random.randint(420, 640), recorder=recorder)
            wait_human(page, plan, i, random.randint(280, 640), recorder=recorder)

        names = _vp_handles(fs, "result_name", limit=12)
        if not names:
            return_home(fs)
            return False
        handle, _box = random.choice(names[: max(1, len(names) // 2) or 1])
        if recorder is not None:
            try:
                recorder.record("interest_open", metadata={"term": term, "surface": "profile"})
            except Exception:
                pass
        click_element(page, handle, plan, recorder=recorder)
        perceive_after_render(page, plan, recorder=recorder)

        if read_sections:
            for i in range(random.randint(2, 4)):
                scroll_page(page, plan, pass_index=i, fallback_delta_y=random.randint(360, 620), recorder=recorder)
                wait_human(page, plan, i, random.randint(320, 760), recorder=recorder)
                for h, box in _vp_handles(fs, "profile_section", limit=1):
                    cx = box["x"] + box["width"] * random.uniform(0.25, 0.6)
                    cy = box["y"] + box["height"] * random.uniform(0.3, 0.6)
                    try:
                        move_pointer(page, cx, cy, plan, recorder=recorder)
                    except Exception:
                        pass

        pb = fs.resolver.one("profile_back")
        if pb is not None:
            try:
                click_element(page, pb, plan, recorder=recorder)
                perceive_after_render(page, plan, recorder=recorder)
            except Exception:
                pass
        return_home(fs)
        return True
    except Exception:
        logger.debug("[feed_actions] search/open profile failed", exc_info=True)
        return_home(fs)
        return False


def lookup_person(fs: FeedSession, name: str) -> bool:
    """Directly check a specific person: search their name, open, read their profile."""
    return search_and_open_profile(fs, name, read_sections=True)


def like_interesting_post(fs: FeedSession) -> bool:
    """Lightweight interest signal: like a visible feed post matching an interest term."""
    low_terms = [t.lower() for t in fs.interest_terms]
    text_cands = fs.resolver.field_candidates("feed_text")
    like_cands = fs.resolver.css_candidates("feed_like")
    for post, _box in _vp_handles(fs, "feed_post", limit=10):
        txt = ""
        te = _within(post, text_cands)
        if te is not None:
            try:
                txt = (te.inner_text() or "")
            except Exception:
                txt = ""
        low = txt.lower()
        if not any(t and t in low for t in low_terms):
            continue
        like = _within(post, like_cands)
        if like is None:
            continue
        try:
            click_element(fs.page, like, fs.plan, recorder=fs.recorder)
            if fs.recorder is not None:
                try:
                    fs.recorder.record("interest_like", metadata={"snippet": txt[:120]})
                except Exception:
                    pass
            return True
        except Exception:
            continue
    return False


def act_on_scored_posts(fs: FeedSession) -> bool:
    """Let the content score drive attention: react to a sufficiently interesting
    on-screen post the way the scorer says (open author on 'click', linger/trace on
    'dwell'/'highlight')."""
    page, plan, recorder, scorer = fs.page, fs.plan, fs.recorder, fs.scorer
    summary, actions = fs.summary, fs.actions
    post_sel = fs.resolver.selector_for("feed_post") or "article"
    author_sel = fs.resolver.field_selector("feed_author") or ".name"
    text_sel = fs.resolver.field_selector("feed_text") or ".text"
    sels = {"post": post_sel, "author": author_sel}
    try:
        onscreen = page.evaluate(
            """(sels) => {
                const vh = innerHeight || 800;
                const out = [];
                document.querySelectorAll(sels.post).forEach(p => {
                    const r = p.getBoundingClientRect();
                    if (r.top < 40 || r.bottom > vh - 20) return;
                    const a = p.querySelector(sels.author);
                    const id = p.getAttribute('data-post-id') || p.getAttribute('data-urn') || p.getAttribute('data-id') || '';
                    out.push({ id: id, author: a ? (a.innerText || '').trim() : '' });
                });
                return out;
            }""",
            sels,
        )
    except Exception:
        return False
    best: tuple[dict[str, Any], dict[str, Any]] | None = None
    for p in onscreen or []:
        s = scorer.score_for(p.get("id")) if isinstance(p, dict) else None
        if s and (best is None or s.get("interest", 0) > best[1].get("interest", 0)):
            best = (p, s)
    if not best or best[1].get("interest", 0) < 0.5:
        return False
    post, score = best
    action = score.get("action", "dwell")
    summary["content_actions"] = summary.get("content_actions", 0) + 1
    if recorder is not None:
        try:
            recorder.record("content_action", metadata={
                "post_id": post.get("id"), "interest": score.get("interest"),
                "action": action, "why": score.get("why", "")})
        except Exception:
            pass
    try:
        if action == "click" and post.get("author"):
            if lookup_person(fs, str(post["author"])):
                summary["profiles_opened"] += 1
                summary["lookups"] += 1
                actions.append(f"content_open:{post['author']}")
                return True
        pid = post.get("id") or ""
        el = None
        if pid:
            try:
                el = page.locator(f'[data-post-id="{pid}"] {text_sel}, [data-urn="{pid}"] {text_sel}').first
                if el.count() == 0:
                    el = None
            except Exception:
                el = None
        if el is not None:
            hover_element(page, el, plan, recorder=recorder)
            wait_human(page, plan, 0, random.randint(500, 1200), recorder=recorder)
        actions.append(f"content_{action}:{score.get('interest')}")
        return True
    except Exception:
        logger.debug("[feed_actions] content action failed", exc_info=True)
        return False


# =========================================================================== #
# Composed units the Story executor calls per scene
# =========================================================================== #
def feed_scan_cycle(fs: FeedSession, *, steps: int | None = None) -> None:
    """One feed-scan scene: ambient Markov burst + capture everything that scrolled
    past + hand to the scorer + content-driven attention + ambient peek/like."""
    n_steps = steps if steps is not None else random.randint(3, 6)
    scan_feed_burst(fs, steps=n_steps)
    fs.summary["feed_scan_bursts"] += 1
    fs.summary["markov_steps"] += n_steps

    new_posts: list[dict[str, Any]] = []
    fs.summary["posts_captured"] += capture_visible_posts(fs, sink=new_posts)
    fs.scorer.submit(new_posts)

    if random.random() < 0.5:
        act_on_scored_posts(fs)
    if random.random() < 0.6 and peek_post_engagement(fs):
        fs.summary["peeks"] += 1
        fs.actions.append("post_peek")
    if random.random() < 0.22 and like_interesting_post(fs):
        fs.summary["likes"] += 1
        fs.actions.append("interest_like")


def do_feed_excursion(fs: FeedSession, name: str, arg: Any = None) -> None:
    """Execute one feed excursion (the human 'check stop'), updating the summary."""
    summary, actions = fs.summary, fs.actions
    if name in ("notifications", "tangent_notifications"):
        if visit_notifications(fs):
            summary["notifications_visited"] += 1
            actions.append("notifications")
    elif name.startswith("glance") or name in ("tangent_glance",):
        surface = name.split(":", 1)[1] if ":" in name else (arg or random.choice(("network", "jobs", "messages")))
        if glance(fs, str(surface)):
            summary["glances"] += 1
            actions.append(f"glance:{surface}")
    elif name in ("search_profile", "tangent_search"):
        term = arg or (random.choice(fs.interest_terms) if fs.interest_terms else "data engineer")
        if search_and_open_profile(fs, str(term), read_sections=True):
            summary["searches"] += 1
            summary["profiles_opened"] += 1
            actions.append(f"search_profile:{term}")
    elif name in ("lookup_person", "tangent_lookup"):
        person = arg
        if not person and fs.pending_lookups:
            person = fs.pending_lookups.pop(0)
        if not person:
            person = random.choice(fs.captured_authors) if fs.captured_authors else None
        if not person:
            term = random.choice(fs.interest_terms) if fs.interest_terms else "data engineer"
            if search_and_open_profile(fs, str(term), read_sections=True):
                summary["searches"] += 1
                summary["profiles_opened"] += 1
                actions.append(f"search_profile:{term}")
        elif lookup_person(fs, str(person)):
            summary["profiles_opened"] += 1
            summary["lookups"] += 1
            actions.append(f"lookup:{person}")
    elif name == "long_browse":
        n_cycles = int(arg) if isinstance(arg, (int, float)) and not isinstance(arg, bool) else random.randint(2, 4)
        for _ in range(n_cycles):
            if fs.time_left <= 0:
                break
            feed_scan_cycle(fs, steps=random.randint(3, 6))
        actions.append(f"long_browse:{n_cycles}")
