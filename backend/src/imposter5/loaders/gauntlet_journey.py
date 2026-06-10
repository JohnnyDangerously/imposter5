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
from imposter5.loaders.content_scorer import ContentScorer
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
    page: Any,
    recorder: SessionRecorder | None,
    captured: set[str],
    authors: list[str] | None = None,
    sink: list[dict[str, Any]] | None = None,
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
                    // Capture is a SIDE EFFECT of viewing, not a sampled event: any
                    // post whose top has crossed above the fold has been scrolled
                    // into view at some point, so capture it. Dedup by post id makes
                    // this idempotent across bursts (and across the feed-resets the
                    // gauntlet does on nav), so the harvest count converges on
                    // "everything actually seen" rather than a per-burst sample.
                    // Only posts still fully below the fold are skipped (not seen yet).
                    if (r.top > vh - 40) return;
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
        if authors is not None:
            author = (p.get("author") or "").strip() if isinstance(p, dict) else ""
            if author:
                authors.append(author)
        if sink is not None and isinstance(p, dict):
            sink.append(p)
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


def _lookup_person(
    page: Any, plan: dict[str, Any] | None, recorder: SessionRecorder | None, name: str
) -> bool:
    """Directly check a specific person: search their name, open the result, read
    their profile. The "I saw their post / someone told me about them, let me look
    them up" action — reuses the search→profile path with the name as the query."""
    return _search_and_open_profile(page, plan, recorder, name, read_sections=True)


def _parse_excursion_queue(
    plan: dict[str, Any] | None,
) -> list[tuple[str, Any]]:
    """Parse the app-supplied "tree of value" into an ordered excursion queue.

    The app can load in concrete human side-quests that take priority over the
    ambient menu, e.g. a list of people to check, an explicit ordered list of
    excursions, or an extra-long browse. Supported plan keys:
      - ``excursion_queue``: list of names ("notifications", "glance:network",
        "long_browse") or dicts ({"type": "lookup_person", "arg": "Jane Doe"}).
      - ``lookup_people``: list of names -> ``lookup_person`` excursions.
      - ``long_browse``: truthy/int -> an extra-long feed-browse excursion.
    """
    queue: list[tuple[str, Any]] = []
    if not isinstance(plan, dict):
        return queue
    raw = plan.get("excursion_queue")
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item.strip():
                queue.append((item.strip(), None))
            elif isinstance(item, dict):
                name = item.get("type") or item.get("name")
                arg = item.get("arg") or item.get("term") or item.get("surface")
                if name == "lookup_person" and not arg:
                    arg = item.get("person") or item.get("name")
                if name:
                    queue.append((str(name), arg))
    people = plan.get("lookup_people")
    if isinstance(people, (list, tuple)):
        for nm in people:
            if str(nm).strip():
                queue.append(("lookup_person", str(nm).strip()))
    if plan.get("long_browse"):
        n = plan.get("long_browse")
        queue.append(("long_browse", int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else 4))
    return queue


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


def _act_on_scored_posts(
    page: Any,
    plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    scorer: ContentScorer,
    summary: dict[str, Any],
    actions: list[str],
) -> bool:
    """Let the content score drive attention: if a sufficiently interesting post
    is on screen, react to it the way the scorer says (open the author on a
    "click", linger/trace on a "dwell"/"highlight"). This is what makes dwell and
    clicks *caused by content* instead of uniform — the whole point of scoring."""
    try:
        onscreen = page.evaluate(
            """() => {
                const vh = innerHeight || 800;
                const out = [];
                document.querySelectorAll('.g-feed-post').forEach(p => {
                    const r = p.getBoundingClientRect();
                    if (r.top < 40 || r.bottom > vh - 20) return;  // fully on screen
                    const nm = p.querySelector('.name');
                    out.push({ id: p.getAttribute('data-post-id'), author: nm ? (nm.innerText || '').trim() : '' });
                });
                return out;
            }"""
        )
    except Exception:
        return False
    best: tuple[dict[str, Any], dict[str, Any]] | None = None
    for p in onscreen or []:
        s = scorer.score_for(p.get("id")) if isinstance(p, dict) else None
        if s and (best is None or s.get("interest", 0) > best[1].get("interest", 0)):
            best = (p, s)
    # Threshold at 0.5 so "dwell"-class interest also reacts; below that is skip.
    if not best or best[1].get("interest", 0) < 0.5:
        return False
    post, score = best
    action = score.get("action", "dwell")
    summary["content_actions"] = summary.get("content_actions", 0) + 1
    if recorder is not None:
        try:
            recorder.record(
                "content_action",
                metadata={"post_id": post.get("id"), "interest": score.get("interest"),
                          "action": action, "why": score.get("why", "")},
            )
        except Exception:
            pass
    try:
        if action == "click" and post.get("author"):
            if _lookup_person(page, plan, recorder, str(post["author"])):
                summary["profiles_opened"] += 1
                summary["lookups"] += 1
                actions.append(f"content_open:{post['author']}")
                return True
        # dwell / highlight: linger on this specific post and trace its text.
        el = page.query_selector(f'[data-post-id="{post.get("id")}"] {FEED_TEXT_SELECTOR}')
        if el is not None:
            hover_element(page, el, plan, recorder=recorder)
            wait_human(page, plan, 0, random.randint(500, 1200), recorder=recorder)
        actions.append(f"content_{action}:{score.get('interest')}")
        return True
    except Exception:
        logger.debug("[gauntlet_journey] content action failed", exc_info=True)
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
        "lookups": 0,
        "likes": 0,
        "glances": 0,
        "duration_s": 0.0,
        "actions": actions,
        "interest_terms": interest_terms,
        "behavior_driver": "aimless_menu_hybrid",
    }
    captured_post_ids: set[str] = set()
    captured_authors: list[str] = []
    # Content scorer: turns the captured feed text into selective attention. The
    # LLM backend runs in a background thread (set IMPOSTER5_CONTENT_EVAL=llm);
    # the default heuristic backend scores instantly in-process.
    persona = None
    if isinstance(behavior_plan, dict):
        persona = behavior_plan.get("persona_description") or behavior_plan.get("persona")
    scorer = ContentScorer(persona)
    summary["content_actions"] = 0

    # The feed view just rendered: pay a floored human perceive latency first
    # (the gauntlet flags sub-90ms reactions as a tell).
    try:
        perceive_after_render(page, behavior_plan, recorder=recorder)
        wait_human(page, behavior_plan, 0, random.randint(600, 1100), recorder=recorder)
    except Exception:
        pass

    # Layer 2 — the app-supplied "tree of value": concrete human side-quests the
    # app loads in (specific people to check, an ordered excursion list, a long
    # browse). These take priority over the ambient menu when present.
    queued_excursions: list[tuple[str, Any]] = _parse_excursion_queue(behavior_plan)

    # Layer 2 fallback — the ambient menu: when nothing is queued, the engine
    # picks a "human thing to do" at weighted random, the way a bored user drifts
    # between checking alerts, glancing at their network, looking someone up, or
    # just browsing more. Weights lean toward light, common actions.
    AMBIENT_MENU: list[tuple[str, float, Any]] = [
        ("long_browse", 3.0, None),      # mostly: just keep browsing the feed
        ("notifications", 2.5, None),    # check alerts
        ("glance", 2.0, None),           # peek at a secondary surface
        ("lookup_person", 1.5, None),    # check someone whose post I saw
        ("search_profile", 1.5, None),   # search an interest and open a profile
    ]
    menu_weights = [m[1] for m in AMBIENT_MENU]

    def _do_excursion(name: str, arg: Any, markov_state: dict[str, Any]) -> dict[str, Any]:
        """Execute one excursion by name; returns the (possibly advanced) markov state."""
        if name == "notifications":
            if _visit_notifications(page, behavior_plan, recorder):
                summary["notifications_visited"] += 1
                actions.append("notifications")
        elif name == "glance" or name.startswith("glance:"):
            surface = name.split(":", 1)[1] if ":" in name else (arg or random.choice(("network", "jobs", "messages")))
            if _glance(page, behavior_plan, recorder, str(surface)):
                summary["glances"] += 1
                actions.append(f"glance:{surface}")
        elif name == "search_profile":
            term = arg or (random.choice(interest_terms) if interest_terms else "data engineer")
            if _search_and_open_profile(page, behavior_plan, recorder, str(term), read_sections=True):
                summary["searches"] += 1
                summary["profiles_opened"] += 1
                actions.append(f"search_profile:{term}")
        elif name == "lookup_person":
            # Prefer someone whose post we actually saw; else an interest search.
            person = arg or (random.choice(captured_authors) if captured_authors else None)
            if not person:
                term = random.choice(interest_terms) if interest_terms else "data engineer"
                if _search_and_open_profile(page, behavior_plan, recorder, str(term), read_sections=True):
                    summary["searches"] += 1
                    summary["profiles_opened"] += 1
                    actions.append(f"search_profile:{term}")
            elif _lookup_person(page, behavior_plan, recorder, str(person)):
                summary["profiles_opened"] += 1
                summary["lookups"] += 1
                actions.append(f"lookup:{person}")
        elif name == "long_browse":
            n_cycles = int(arg) if isinstance(arg, (int, float)) and not isinstance(arg, bool) else random.randint(2, 4)
            for _ in range(n_cycles):
                if time.monotonic() - start >= duration_s:
                    break
                ms = random.randint(3, 6)
                markov_state = _scan_feed_burst(page, behavior_plan, recorder, markov_state, steps=ms)
                summary["feed_scan_bursts"] += 1
                summary["markov_steps"] += ms
                lb_posts: list[dict[str, Any]] = []
                summary["posts_captured"] += _capture_visible_posts(
                    page, recorder, captured_post_ids, captured_authors, sink=lb_posts
                )
                scorer.submit(lb_posts)
            actions.append(f"long_browse:{n_cycles}")
        return markov_state

    start = time.monotonic()
    markov_state: dict[str, Any] = {}
    loops = 0
    # Layer 1 — the base loop is AIMLESS feed browsing (the default human mode:
    # "I don't really know what I'm doing, just looking"). A purposeful excursion
    # only fires every few scan cycles; the long uninterrupted scroll both reads
    # as natural and lets capture reach deep, fresh posts (excursions reset the
    # feed to the top on the gauntlet).
    # When the app has loaded a value tree, drain it on a tighter cadence (those
    # are things it explicitly wants done); with nothing queued, excursions are
    # rare and aimless browsing dominates.
    cycles_per_excursion = random.randint(1, 2) if queued_excursions else random.randint(4, 6)
    while time.monotonic() - start < duration_s:
        loops += 1
        # Ambient feed scan burst (this is the scroll) — the dominant activity.
        steps = random.randint(3, 6)
        markov_state = _scan_feed_burst(page, behavior_plan, recorder, markov_state, steps=steps)
        summary["feed_scan_bursts"] += 1
        summary["markov_steps"] += steps

        # Capture everything that just scrolled past — the actual data job — and
        # hand the new posts to the scorer (instant heuristic, or queued for the
        # background LLM pass).
        new_posts: list[dict[str, Any]] = []
        summary["posts_captured"] += _capture_visible_posts(
            page, recorder, captured_post_ids, captured_authors, sink=new_posts
        )
        scorer.submit(new_posts)

        # Content-driven attention: react to a genuinely interesting on-screen post.
        if random.random() < 0.5 and _act_on_scored_posts(page, behavior_plan, recorder, scorer, summary, actions):
            pass

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

        # Only take an excursion every few scan cycles (aimless browsing dominates).
        if loops % cycles_per_excursion != 0:
            continue

        # Prefer the app's queued side-quests; otherwise drift to an ambient one.
        if queued_excursions:
            name, arg = queued_excursions.pop(0)
        else:
            name, arg = random.choices(AMBIENT_MENU, weights=menu_weights, k=1)[0][0], None
        markov_state = _do_excursion(name, arg, markov_state)
        # Next cadence: keep draining a loaded tree quickly (purposeful session
        # with a little browsing between tasks), else go rare again (aimless).
        cycles_per_excursion = random.randint(1, 2) if queued_excursions else random.randint(4, 6)

    summary["queued_remaining"] = len(queued_excursions)
    summary["content_eval"] = scorer.stats()
    summary["duration_s"] = round(time.monotonic() - start, 1)
    if recorder is not None:
        try:
            summary["session_recording"] = recorder.payload()
        except Exception:
            summary["session_recording"] = None
    logger.info(
        "[gauntlet_journey] done in %.1fs: scans=%d captured=%d peeks=%d profiles=%d lookups=%d notifs=%d glances=%d likes=%d",
        summary["duration_s"], summary["feed_scan_bursts"], summary["posts_captured"],
        summary["peeks"], summary["profiles_opened"], summary["lookups"],
        summary["notifications_visited"], summary["glances"], summary["likes"],
    )
    return summary
