"""Gauntlet journey runner — drives the Blue Team "gauntlet" field with the full
improved Red suite so it scores well on the Blue evasion detector AND records
into the imposter session (video + playback) like a LinkedIn run.

The gauntlet (last-human-line `/gauntlet`) is a LinkedIn-PROPORTIONED single-page
field: a long scrollable feed, a notifications panel, search -> results ->
profile navigation, and other nav surfaces. It is *proportioned* like LinkedIn
but not fully functional — feed posts don't open to a detail view — so the
"human interest" behavior maps to the gauntlet's real endgame: search an
interest term, open an interesting profile from the results, and read its
sections (plus an occasional like on a relevant feed post).

This module owns NO motor model of its own: every move/scroll/click/type/dwell
goes through the shared humanized primitives, and the ambient feed scan is
driven by the same semi-Markov engine (with burst continuity) used for the
LinkedIn hybrid. The gauntlet captures its own Event Vocabulary v2 telemetry
from the real DOM events these primitives dispatch, then scores the traversal.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

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
from imposter5.loaders.linkedin_feed_scraper import FEED_SCAN_MATRIX
from imposter5.loaders.markov_simulator import run_markov_simulation

logger = logging.getLogger(__name__)

# --- Gauntlet DOM contract (docs/gauntlet-layout-v1.md) --------------------- #
FEED_POST_SELECTOR = "article.g-feed-post, .g-feed-post"
FEED_LIKE_SELECTOR = ".g-feed-like"
FEED_TEXT_SELECTOR = ".text"
FEED_ACTIONS_SELECTOR = ".actions"
FEED_NAME_SELECTOR = ".name"
NAV = {
    "home": "#g-nav-home",
    "notifications": "#g-nav-notifications",
    "messages": "#g-nav-messages",
    "network": "#g-nav-network",
    "jobs": "#g-nav-jobs",
    "me": "#g-nav-me",
}
SEARCH_INPUT = "#g-search-input"
SEARCH_GO = "#g-search-go"
RESULT_NAME_SELECTOR = ".g-result-name"
RESULT_CARD_SELECTOR = ".g-result-card"
PROFILE_BACK = "#g-profile-back"
PROFILE_SECTION_SELECTOR = ".g-profile-section"
NOTIF_PANEL = "#g-view-notifications"

# Where the ambient Markov scan is allowed to aim its hovers/moves on the
# gauntlet — real feed content, never arbitrary viewport coordinates.
_SCAN_TARGETS: tuple[str, ...] = (FEED_POST_SELECTOR,)

# Default interest vocabulary tuned to the gauntlet's data-engineering themed
# content (used when the run carries no explicit ICP/interest terms). A human
# would search these and open a matching profile, not scan every card.
_DEFAULT_GAUNTLET_INTERESTS: tuple[str, ...] = (
    "data engineer",
    "ml platform",
    "analytics lead",
    "staff data engineer",
    "head of data",
)


def _resolve_interest_terms(plan: dict[str, Any] | None) -> list[str]:
    """ICP / interest terms from the plan, else a gauntlet-appropriate default."""
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
    if not terms:
        terms = list(_DEFAULT_GAUNTLET_INTERESTS)
    return terms


def _visible_handles(page: Any, selector: str, *, limit: int = 25) -> list[tuple[Any, dict]]:
    """Visible, in-viewport (handle, box) pairs for a selector (best-effort)."""
    out: list[tuple[Any, dict]] = []
    try:
        handles = page.query_selector_all(selector) or []
    except Exception:
        return out
    for h in handles[: limit * 3]:
        try:
            box = h.bounding_box()
        except Exception:
            box = None
        if not box or box["width"] < 30 or box["height"] < 16:
            continue
        if box["y"] < -120 or box["y"] > 1200:
            continue
        out.append((h, box))
        if len(out) >= limit:
            break
    return out


def _scan_feed_burst(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    state: dict[str, Any],
    *,
    steps: int,
) -> dict[str, Any]:
    """One semi-Markov ambient scan burst over the feed, threading the walk state
    forward so the whole session reads as one continuous scan (no per-burst
    settle/idle preamble) and aiming hovers/moves at real feed posts."""
    burst_plan = dict(plan or {})
    burst_plan["markov_matrix"] = FEED_SCAN_MATRIX
    try:
        return run_markov_simulation(
            page,
            burst_plan,
            recorder=recorder,
            max_steps=steps,
            initial_state=state.get("final_state"),
            initial_intent=state.get("final_intent"),
            intent_steps_left=state.get("intent_steps_left"),
            suppress_intro_wait=bool(state),
            mousemove_targets=_SCAN_TARGETS,
            hover_targets=_SCAN_TARGETS,
        )
    except Exception:
        logger.debug("[gauntlet_journey] feed scan burst failed", exc_info=True)
        # Never let a scan stall the journey: do one deterministic scroll.
        try:
            scroll_page(page, plan, pass_index=0, fallback_delta_y=720, recorder=recorder)
        except Exception:
            pass
        return state


def _capture_visible_posts(
    page: Any, recorder: SessionRecorder | None, captured: set[str]
) -> int:
    """Capture every feed post currently on screen — the actual data-gathering job.

    A scraping human's whole point is to harvest content as it scrolls past; doing
    this in a single ``page.evaluate`` over the visible posts means capture keeps
    pace with the scroll (every post that enters the viewport is recorded once)
    instead of trickling out behind sparse goal steps. This is a pure DOM read —
    it dispatches no input events, so it adds rich evidence without changing the
    behavioral surface the Blue detector sees.
    """
    try:
        posts = page.evaluate(
            """() => {
                const vh = innerHeight || 800;
                const out = [];
                document.querySelectorAll('.g-feed-post').forEach(p => {
                    const r = p.getBoundingClientRect();
                    // Capture posts on screen OR ones that just scrolled past (up to
                    // ~700px above the fold) — a fast scroll sweeps several posts
                    // between bursts and a reader still "sees" them go by, so a
                    // capture window wider than the literal viewport keeps harvest
                    // rate matched to scroll speed. Below the fold is skipped.
                    if (r.bottom < -700 || r.top > vh - 40) return;
                    const pick = (s) => { const e = p.querySelector(s); return e ? (e.innerText || '').trim() : ''; };
                    out.push({
                        id: p.getAttribute('data-post-id'),
                        author: pick('.name'),
                        headline: pick('.meta'),
                        text: pick('.text').slice(0, 200),
                    });
                });
                return out;
            }"""
        )
    except Exception:
        return 0
    n = 0
    for p in posts or []:
        pid = p.get("id") if isinstance(p, dict) else None
        if not pid or pid in captured:
            continue
        captured.add(pid)
        n += 1
        if recorder is not None:
            try:
                recorder.record(
                    "feed_capture",
                    metadata={
                        "post_id": pid,
                        "author": p.get("author", ""),
                        "headline": p.get("headline", ""),
                        "snippet": (p.get("text") or "")[:120],
                    },
                )
            except Exception:
                pass
    return n


def _peek_post_engagement(
    page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None
) -> bool:
    """Glance at a post's comments / reactions: hover the action row (or author)
    and dwell briefly, the way a reader peeks at engagement before moving on.

    This is the ambient "looking at comments" micro-behavior that breaks up a
    pure scroll, aiming the cursor at real in-post controls rather than empty
    viewport space."""
    posts = _visible_handles(page, FEED_POST_SELECTOR, limit=8)
    if not posts:
        return False
    post, _box = random.choice(posts)
    target = None
    # Prefer the comment/reaction row; fall back to author, then body text.
    for sel in (FEED_ACTIONS_SELECTOR, FEED_NAME_SELECTOR, FEED_TEXT_SELECTOR):
        try:
            t = post.query_selector(sel)
        except Exception:
            t = None
        if t is not None:
            target = t
            break
    if target is None:
        return False
    try:
        update_status_ticker(page, "💬 PEEKING", "Glancing at comments / reactions...")
        hover_element(page, target, plan, recorder=recorder)
        wait_human(page, plan, 0, random.randint(220, 620), recorder=recorder)
        if recorder is not None:
            try:
                recorder.record("post_peek", metadata={})
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("[gauntlet_journey] post-engagement peek failed", exc_info=True)
        return False


def _return_home(page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None) -> None:
    try:
        click_element(page, NAV["home"], plan, recorder=recorder)
        perceive_after_render(page, plan, recorder=recorder)
    except Exception:
        logger.debug("[gauntlet_journey] return-home failed", exc_info=True)


def _visit_notifications(
    page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None
) -> bool:
    """Open notifications, read down the (150-entry) list, then back to the feed."""
    try:
        update_status_ticker(page, "🔔 NOTIFICATIONS", "Checking notifications...")
        click_element(page, NAV["notifications"], plan, recorder=recorder)
        perceive_after_render(page, plan, recorder=recorder)
        for i in range(random.randint(2, 4)):
            scroll_page(page, plan, pass_index=i, fallback_delta_y=random.randint(420, 700), recorder=recorder)
            wait_human(page, plan, i, random.randint(280, 720), recorder=recorder)
        if recorder is not None:
            try:
                recorder.record("notifications_visit", metadata={})
            except Exception:
                pass
        _return_home(page, plan, recorder)
        return True
    except Exception:
        logger.debug("[gauntlet_journey] notifications visit failed", exc_info=True)
        _return_home(page, plan, recorder)
        return False


def _glance(
    page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None, surface: str
) -> bool:
    """Quick human glance at a secondary nav surface (network/jobs/messages)."""
    sel = NAV.get(surface)
    if not sel:
        return False
    try:
        update_status_ticker(page, "🧭 BROWSING", f"Glancing at {surface}...")
        click_element(page, sel, plan, recorder=recorder)
        perceive_after_render(page, plan, recorder=recorder)
        for i in range(random.randint(1, 2)):
            scroll_page(page, plan, pass_index=i, fallback_delta_y=random.randint(360, 560), recorder=recorder)
            wait_human(page, plan, i, random.randint(240, 560), recorder=recorder)
        _return_home(page, plan, recorder)
        return True
    except Exception:
        logger.debug("[gauntlet_journey] glance at %s failed", surface, exc_info=True)
        _return_home(page, plan, recorder)
        return False


def _search_and_open_profile(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    term: str,
    *,
    read_sections: bool,
) -> bool:
    """The gauntlet's human-interest endgame: search an interest term, scan the
    results, open an interesting profile, read a few of its sections, then back."""
    try:
        update_status_ticker(page, "🔎 SEARCHING", f"Searching for '{term}'...")
        click_element(page, SEARCH_INPUT, plan, recorder=recorder)
        wait_human(page, plan, 0, random.randint(160, 380), recorder=recorder)
        type_text(page, SEARCH_INPUT, term, plan, recorder=recorder)
        wait_human(page, plan, 0, random.randint(180, 420), recorder=recorder)
        click_element(page, SEARCH_GO, plan, recorder=recorder)
        perceive_after_render(page, plan, recorder=recorder)

        # Scan ~half the results (the gauntlet's scan_fraction endgame).
        for i in range(random.randint(2, 4)):
            scroll_page(page, plan, pass_index=i, fallback_delta_y=random.randint(420, 640), recorder=recorder)
            wait_human(page, plan, i, random.randint(280, 640), recorder=recorder)

        names = _visible_handles(page, RESULT_NAME_SELECTOR, limit=12)
        if not names:
            _return_home(page, plan, recorder)
            return False
        # Open one of the visible results (a person who caught the eye).
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
                for h, box in _visible_handles(page, PROFILE_SECTION_SELECTOR, limit=1):
                    cx = box["x"] + box["width"] * random.uniform(0.25, 0.6)
                    cy = box["y"] + box["height"] * random.uniform(0.3, 0.6)
                    try:
                        move_pointer(page, cx, cy, plan, recorder=recorder)
                    except Exception:
                        pass

        # Back to results, then home (purposeful human backtrack).
        try:
            click_element(page, PROFILE_BACK, plan, recorder=recorder)
            perceive_after_render(page, plan, recorder=recorder)
        except Exception:
            pass
        _return_home(page, plan, recorder)
        return True
    except Exception:
        logger.debug("[gauntlet_journey] search/open profile failed", exc_info=True)
        _return_home(page, plan, recorder)
        return False


def _like_interesting_post(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    interest_terms: list[str],
) -> bool:
    """Lightweight interest signal: like a visible feed post whose text matches an
    interest term (the only meaningful in-feed action the gauntlet exposes)."""
    low_terms = [t.lower() for t in interest_terms]
    for post, _box in _visible_handles(page, FEED_POST_SELECTOR, limit=10):
        try:
            text_el = post.query_selector(FEED_TEXT_SELECTOR)
            txt = (text_el.inner_text() if text_el else "") or ""
        except Exception:
            txt = ""
        low = txt.lower()
        if not any(t and t in low for t in low_terms):
            continue
        try:
            like = post.query_selector(FEED_LIKE_SELECTOR)
            if not like:
                continue
            click_element(page, like, plan, recorder=recorder)
            if recorder is not None:
                try:
                    recorder.record("interest_like", metadata={"snippet": txt[:120]})
                except Exception:
                    pass
            return True
        except Exception:
            continue
    return False


def run_gauntlet_journey(
    page: Any,
    behavior_plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
    interest_terms: list[str] | None = None,
    duration_s: float = 240.0,
    seed: int | None = None,
) -> dict[str, Any]:
    """Drive a human-like, multi-minute journey across the gauntlet: an ambient
    Markov-continuity feed scan interleaved with purposeful goal actions
    (notifications, interest-driven search -> profile reads, secondary-surface
    glances, an occasional like). Duration-bounded so it runs for minutes and
    produces a rich journey for the Blue scorer. Returns a journey summary."""
    if seed is not None:
        random.seed(seed)
    interest_terms = interest_terms or _resolve_interest_terms(behavior_plan)
    actions: list[str] = []
    summary = {
        "feed_scan_bursts": 0,
        "markov_steps": 0,
        "posts_captured": 0,
        "peeks": 0,
        "notifications_visited": 0,
        "profiles_opened": 0,
        "searches": 0,
        "likes": 0,
        "glances": 0,
        "duration_s": 0.0,
        "actions": actions,
        "interest_terms": interest_terms,
        "behavior_driver": "markov_goal_hybrid",
    }
    captured_post_ids: set[str] = set()

    # The feed view just rendered: pay a floored human perceive latency first
    # (the gauntlet flags sub-90ms reactions as a tell).
    try:
        perceive_after_render(page, behavior_plan, recorder=recorder)
        wait_human(page, behavior_plan, 0, random.randint(600, 1100), recorder=recorder)
    except Exception:
        pass

    # A loose, varied goal sequence. Every loop already does an ambient scan
    # burst before pulling the next goal, so we do NOT pad this with standalone
    # "scan" steps — those just double the dead scrolling and push the
    # data-gathering goals (profile reads, searches) further apart. Keep it dense
    # with capture-rich goals so evidence accumulates quickly.
    goal_queue: list[tuple[str, Any]] = [
        ("search_profile", interest_terms[0] if interest_terms else "data engineer"),
        ("notifications", None),
        ("glance", "network"),
        ("search_profile", interest_terms[min(1, len(interest_terms) - 1)] if interest_terms else "analytics"),
        ("glance", "jobs"),
        ("notifications", None),
        ("glance", "messages"),
    ]

    start = time.monotonic()
    markov_state: dict[str, Any] = {}
    qi = 0
    loops = 0
    # A goal excursion (notifications/search/glance) navigates away and the
    # gauntlet resets the feed to the top, so each excursion costs capture depth.
    # Spend most cycles scrolling+capturing the feed and only take an excursion
    # every few cycles: this keeps data capture fast (long uninterrupted harvest
    # runs that reach deep, fresh posts) while still varying the journey.
    cycles_per_goal = random.randint(2, 3)
    while time.monotonic() - start < duration_s:
        loops += 1
        # Ambient feed scan burst (this is the scroll) — the dominant activity.
        steps = random.randint(3, 6)
        markov_state = _scan_feed_burst(page, behavior_plan, recorder, markov_state, steps=steps)
        summary["feed_scan_bursts"] += 1
        summary["markov_steps"] += steps

        # Capture everything that just scrolled past — the actual data job.
        summary["posts_captured"] += _capture_visible_posts(page, recorder, captured_post_ids)

        # Ambient variety: sometimes peek at a post's comments/reactions, sometimes
        # like a relevant one. These are independent so the rhythm stays irregular.
        if random.random() < 0.6 and _peek_post_engagement(page, behavior_plan, recorder):
            summary["peeks"] += 1
            actions.append("post_peek")
        if random.random() < 0.22 and _like_interesting_post(page, behavior_plan, recorder, interest_terms):
            summary["likes"] += 1
            actions.append("interest_like")

        if time.monotonic() - start >= duration_s:
            break

        # Only take a purposeful goal excursion every few scan cycles.
        if loops % cycles_per_goal != 0:
            continue
        cycles_per_goal = random.randint(2, 3)

        # Pull the next purposeful goal (cycle if the journey runs long).
        if qi >= len(goal_queue):
            # Refill with capture-rich goals (no empty scan padding) so long runs
            # keep gathering data and stay varied.
            goal_queue.append(("search_profile", random.choice(interest_terms) if interest_terms else "data engineer"))
            goal_queue.append(("glance", random.choice(("network", "jobs", "messages"))))
            goal_queue.append(("notifications", None))
        kind, arg = goal_queue[qi]
        qi += 1

        if kind == "scan":
            continue
        elif kind == "notifications":
            if _visit_notifications(page, behavior_plan, recorder):
                summary["notifications_visited"] += 1
                actions.append("notifications")
        elif kind == "glance":
            if _glance(page, behavior_plan, recorder, arg):
                summary["glances"] += 1
                actions.append(f"glance:{arg}")
        elif kind == "search_profile":
            if _search_and_open_profile(page, behavior_plan, recorder, str(arg), read_sections=True):
                summary["searches"] += 1
                summary["profiles_opened"] += 1
                actions.append(f"search_profile:{arg}")

    summary["duration_s"] = round(time.monotonic() - start, 1)
    if recorder is not None:
        try:
            summary["session_recording"] = recorder.payload()
        except Exception:
            summary["session_recording"] = None
    logger.info(
        "[gauntlet_journey] done in %.1fs: scans=%d captured=%d peeks=%d profiles=%d notifs=%d glances=%d likes=%d",
        summary["duration_s"], summary["feed_scan_bursts"], summary["posts_captured"],
        summary["peeks"], summary["profiles_opened"], summary["notifications_visited"],
        summary["glances"], summary["likes"],
    )
    return summary
