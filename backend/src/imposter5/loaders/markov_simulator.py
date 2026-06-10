"""Semi-Markov Pathing Simulator for Imposter5.

Generates dynamic, non-linear, probabilistic browsing sessions based on a
transition probability matrix plus an explicit per-state dwell (sojourn) time.
This makes the process a *semi-Markov* one: the next state is chosen from the
transition matrix, and the time spent in that state is sampled from a per-state
log-normal sojourn distribution rather than a flat uniform draw.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from imposter5.automation_connector.humanize_dist import lognormal_ms, weibull_ms
from imposter5.automation_connector.interaction_primitives import (
    _get_cursor,
    _session_rng,
    click_element,
    hover_element,
    move_pointer,
    perceive_after_render,
    scroll_page,
    trace_text_selection,
    type_text,
    update_status_ticker,
    wait_human,
)
from imposter5.automation_connector.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)

# Default human transition matrix — hand-tuned defaults (a hand-typed constant,
# not learned from data). Each row is the distribution over next states given
# the current state; rows are re-normalized at runtime so small edits stay safe.
# A human cruising a feed overwhelmingly scrolls DOWN, with brief glances and
# the occasional pause. Scrolling back UP is rare (you re-read something you
# just passed once in a while) and is NEVER sticky — after a single up-glance
# you go back down. scroll_down is the dominant, self-reinforcing state so the
# walk reads as steady forward progress, not a hesitant down-a-little / back-up
# / pause loop (which both looks wrong on video and is an easy detection signal).
DEFAULT_HUMAN_MATRIX = {
    "idle": {
        "idle": 0.10,
        "mousemove": 0.22,
        "scroll_down": 0.50,
        "scroll_up": 0.02,
        "hover": 0.10,
        "click": 0.03,
        "typing": 0.03
    },
    "mousemove": {
        "idle": 0.12,
        "mousemove": 0.15,
        "scroll_down": 0.48,
        "scroll_up": 0.02,
        "hover": 0.16,
        "click": 0.05,
        "typing": 0.02
    },
    "scroll_down": {
        "idle": 0.14,
        "mousemove": 0.12,
        "scroll_down": 0.62,
        "scroll_up": 0.02,
        "hover": 0.06,
        "click": 0.03,
        "typing": 0.01
    },
    "scroll_up": {
        # Not sticky: after a single re-read glance, resume scrolling down.
        "idle": 0.12,
        "mousemove": 0.14,
        "scroll_down": 0.62,
        "scroll_up": 0.02,
        "hover": 0.06,
        "click": 0.03,
        "typing": 0.01
    },
    "hover": {
        "idle": 0.18,
        "mousemove": 0.18,
        "scroll_down": 0.40,
        "scroll_up": 0.01,
        "hover": 0.10,
        "click": 0.10,
        "typing": 0.03
    },
    "click": {
        "idle": 0.34,
        "mousemove": 0.18,
        "scroll_down": 0.38,
        "scroll_up": 0.01,
        "hover": 0.06,
        "click": 0.01,
        "typing": 0.02
    },
    "typing": {
        "idle": 0.20,
        "mousemove": 0.13,
        "scroll_down": 0.10,
        "scroll_up": 0.01,
        "hover": 0.05,
        "click": 0.47,
        "typing": 0.04
    }
}

# Per-state sojourn-time table for the semi-Markov process: (mean_ms, cv).
# Hand-tuned defaults: idle/reading dwells longest, clicks are quick, typing
# and scrolling sit in the middle. Sampled via a log-normal so the long tail
# (occasional long pauses) looks human rather than uniformly bounded.
# Per-state sojourn means. Tuned snappy: glancing at a feed post takes ~1-3s
# (Brysbaert reading rates over a visible snippet), not many seconds. Long
# engagement only happens when the goal layer opens a post. Over-long idle
# pauses both look unnatural on video and read as hesitation to the scorer.
STATE_DWELL_MS: dict[str, tuple[float, float]] = {
    "idle": (1300.0, 0.55),
    "mousemove": (380.0, 0.45),
    "scroll_down": (620.0, 0.50),
    "scroll_up": (560.0, 0.50),
    "hover": (560.0, 0.45),
    "click": (360.0, 0.40),
    "typing": (1100.0, 0.50),
}

# Short transition gap between leaving one state and entering the next.
INTER_STEP_MEAN_MS = 320.0
INTER_STEP_CV = 0.45

# Global clamps so log-normal tails stay plausible. The dwell ceiling is kept
# modest so an unlucky log-normal tail can't manufacture a 10s+ stare at the
# feed (a human scanning rarely pauses that long without engaging).
DWELL_LO_MS = 80
DWELL_HI_MS = 5_500
GAP_HI_MS = 3_000

TYPING_QUERIES = ("hello", "markov chains", "last human line", "cybersecurity", "evasion")

# --- Hierarchical goal/intent layer ----------------------------------------------
#
# Real browsing is not a flat metronomic move/pause loop: it is organized into
# higher-level INTENTS ("read this for a bit", "scan down the page", "engage with an
# element", "fill a field") that each bias the low-level action mix for a stretch and
# then hand off to another intent. This is a hierarchical (goal -> subaction) model,
# and the intent layer is itself a semi-Markov process (each intent has a sampled
# sojourn in sub-steps before switching). The result is structurally human bursts of
# related activity whose OBSERVABLE event mix differs every run, instead of a fixed
# action chain. Models of web behavior describe exactly these higher-order "browsing
# states"/sessions over primitive events (e.g. Montgomery et al., 2004, *Marketing
# Science*, on clickstream "browsing states"; Catledge & Pitkow, 1995).
#
# Each intent supplies multiplicative biases applied to the base transition row and
# then renormalized, so the underlying transition structure is preserved while the
# emphasis shifts.
INTENT_BIAS: dict[str, dict[str, float]] = {
    "reading": {"idle": 2.4, "scroll_down": 1.4, "scroll_up": 0.4, "mousemove": 0.7, "hover": 0.6, "click": 0.3, "typing": 0.2},
    "scanning": {"idle": 0.6, "scroll_down": 1.9, "mousemove": 1.6, "scroll_up": 0.3, "hover": 0.9, "click": 0.5, "typing": 0.2},
    "engaging": {"idle": 0.8, "hover": 1.9, "click": 1.9, "mousemove": 1.3, "scroll_down": 0.7, "scroll_up": 0.3, "typing": 0.6},
    "form_filling": {"typing": 3.2, "click": 1.6, "hover": 1.2, "mousemove": 1.0, "idle": 0.7, "scroll_down": 0.4, "scroll_up": 0.2},
}

# Intent-to-intent transition weights (the higher-level semi-Markov chain). Rows are
# renormalized at runtime; diagonal weight gives natural "stickiness" so an intent
# persists for a human-plausible burst before switching.
INTENT_TRANSITIONS: dict[str, dict[str, float]] = {
    "reading": {"reading": 0.45, "scanning": 0.35, "engaging": 0.15, "form_filling": 0.05},
    "scanning": {"scanning": 0.40, "reading": 0.30, "engaging": 0.25, "form_filling": 0.05},
    "engaging": {"engaging": 0.35, "reading": 0.30, "scanning": 0.25, "form_filling": 0.10},
    "form_filling": {"form_filling": 0.40, "engaging": 0.30, "reading": 0.20, "scanning": 0.10},
}

# Per-intent sojourn in sub-steps (mean, cv) for a log-normal draw.
INTENT_SOJOURN_STEPS: dict[str, tuple[float, float]] = {
    "reading": (4.0, 0.5),
    "scanning": (3.5, 0.5),
    "engaging": (3.0, 0.6),
    "form_filling": (4.5, 0.5),
}

# Occasional distraction/idle: a heavy-tailed Weibull gap modelling the operator
# briefly looking away (shape < 1 => heavy tail of rare long pauses).
DISTRACTION_CHANCE = 0.06
DISTRACTION_SCALE_MS = 2_200.0
DISTRACTION_SHAPE = 0.8
DISTRACTION_HI_MS = 30_000.0


def _visible_text_amount(page: Any) -> int:
    """Best-effort character count of the currently visible body text.

    Used to correlate reading-pause length with how much content is on screen:
    humans dwell longer on text-dense views. Silent reading proceeds at ~238 wpm
    (Brysbaert, 2019), i.e. a few hundred ms per word. Failures return 0 (honest
    "unknown"), which leaves the base dwell unscaled rather than fabricating content.

    Counts only text of block elements intersecting the CURRENT VIEWPORT, not the
    whole document. A human's reading pause tracks what is on screen; measuring the
    entire body (e.g. a 500-post / infinite feed) pins the dwell at its ceiling and
    makes every scan step pause for many seconds — both unnaturally slow and a tell.
    """
    try:
        n = page.evaluate(
            """() => {
                const vh = window.innerHeight, vw = window.innerWidth;
                let total = 0, seen = 0;
                const els = document.querySelectorAll('article, p, li, h1, h2, h3, .text');
                for (let i = 0; i < els.length; i++) {
                    const r = els[i].getBoundingClientRect();
                    if (r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw && r.height > 0) {
                        total += (els[i].innerText || '').length;
                        if (++seen > 60) break;
                    }
                }
                return total;
            }"""
        )
        return int(n or 0)
    except Exception:
        return 0


def _reading_scale(char_count: int) -> float:
    """Map visible-text amount to a dwell multiplier in ~[0.7, 1.8].

    Roughly: sparse pages read faster (less to take in), dense pages slower. The
    log keeps the scaling sub-linear so a very long page does not produce an absurd
    pause. Anchored so ~600 chars (a typical visible paragraph) maps near 1.0.
    """
    if char_count <= 0:
        return 1.0
    scale = 0.7 + 0.15 * math.log2(1.0 + char_count / 150.0)
    return max(0.7, min(1.35, scale))


def _choose_intent(rng: Any, current: str) -> str:
    row = INTENT_TRANSITIONS.get(current, INTENT_TRANSITIONS["reading"])
    intents = list(row.keys())
    weights = list(row.values())
    total = sum(weights) or 1.0
    weights = [w / total for w in weights]
    return rng.choices(intents, weights=weights, k=1)[0]


def _biased_row(base_row: dict[str, float], intent: str) -> tuple[list[str], list[float]]:
    """Apply the current intent's multiplicative bias to a base transition row."""
    bias = INTENT_BIAS.get(intent, {})
    states = list(base_row.keys())
    weights = [float(base_row[s]) * float(bias.get(s, 1.0)) for s in states]
    total = sum(weights)
    if total <= 0:
        # Degenerate after biasing: fall back to the unbiased row rather than guessing.
        weights = [float(base_row[s]) for s in states]
        total = sum(weights) or 1.0
    return states, [w / total for w in weights]


def _normalize_matrix(matrix: dict[str, Any], *, source: str) -> dict[str, dict[str, float]]:
    """Validate each transition row and return a normalized copy.

    Rows that do not sum to ~1.0 are normalized and reported (logged); rows that
    are empty or non-positive are a real defect and raise rather than silently
    degrade to a fabricated distribution.
    """
    normalized: dict[str, dict[str, float]] = {}
    for state, probs in matrix.items():
        if not isinstance(probs, dict) or not probs:
            raise ValueError(f"markov_matrix row '{state}' is empty or not a mapping")
        total = sum(float(v) for v in probs.values())
        if total <= 0:
            raise ValueError(f"markov_matrix row '{state}' sums to {total}; cannot normalize")
        if abs(total - 1.0) > 1e-6:
            logger.warning(
                "[markov_simulator] %s matrix row '%s' sums to %.4f; normalizing to 1.0",
                source, state, total,
            )
            normalized[state] = {k: float(v) / total for k, v in probs.items()}
        else:
            normalized[state] = {k: float(v) for k, v in probs.items()}
    return normalized


def _sample_dwell_ms(rng: Any, state: str) -> int:
    mean_ms, cv = STATE_DWELL_MS.get(state, STATE_DWELL_MS["idle"])
    return int(lognormal_ms(rng, mean_ms=mean_ms, cv=cv, lo=DWELL_LO_MS, hi=DWELL_HI_MS))


def _viewport_wh(page: Any) -> tuple[int, int]:
    """Current viewport size in CSS px, with a sane desktop fallback."""
    try:
        vp = page.viewport_size or {}
        vw = int(vp.get("width") or 0)
        vh = int(vp.get("height") or 0)
        if vw > 0 and vh > 0:
            return vw, vh
    except Exception:
        pass
    return 1280, 800


def _box_in_viewport(box: Any, vw: int, vh: int, *, margin: int = 8) -> bool:
    """True when an element's box intersects the current viewport.

    Playwright reports ``bounding_box`` relative to the viewport top-left, so an
    element scrolled below the fold has ``y`` >= ``vh`` (we saw y≈2532 on an
    800px viewport). Requiring intersection here is what keeps the ambient
    cursor on-screen: a human never points at or hovers content they have not
    scrolled into view, and an off-screen ``mouse.move`` is a hard tell.
    """
    if not box:
        return False
    try:
        top, bottom = box["y"], box["y"] + box["height"]
        left, right = box["x"], box["x"] + box["width"]
    except Exception:
        return False
    return (bottom > margin and top < vh - margin
            and right > margin and left < vw - margin)


def _pick_visible_locator(page: Any, rng: Any, selector: str, *, limit: int = 40) -> Any | None:
    """Return a single in-viewport element handle chosen with the seeded rng, or None.

    Resolving the multi-match selector to one concrete (`.first`-friendly) element
    handle here keeps the downstream click/hover primitives unambiguous and lets
    the seeded rng own the link pick for run reproducibility. Only elements that
    actually intersect the current viewport are eligible: ``is_visible`` alone is
    true for DOM-visible content below the fold, which would aim the cursor at
    off-screen coordinates.
    """
    try:
        candidates = page.locator(selector).all()
    except Exception:
        return None
    vw, vh = _viewport_wh(page)
    visible = []
    for loc in candidates[:limit]:
        try:
            if not loc.is_visible() or not loc.is_enabled():
                # Skip hidden and disabled controls (e.g. the harness auto-submit
                # button is disabled; clicking it just burns the action timeout).
                continue
            el_id = (loc.get_attribute("id") or "").lower()
            if "submit" in el_id or "honeypot" in el_id:
                # Never let the random walk fire the form submit or a honeypot
                # trap; submit timing and trap evasion are owned elsewhere.
                continue
            if not _box_in_viewport(loc.bounding_box(), vw, vh):
                continue
            visible.append(loc)
        except Exception:
            continue
    if not visible:
        return None
    return rng.choice(visible)


def _content_move_target(page: Any, rng: Any, targets: tuple[str, ...] | None) -> tuple[int, int]:
    """Pick a mouse-move destination over real content when ``targets`` is given.

    Aiming ambient ``mousemove`` steps at an actual on-page element (e.g. a feed
    post) keeps the random walk consistent with the goal layer's content-relative
    moves instead of jumping to arbitrary viewport coordinates (a tell). Falls
    back to a jittered viewport point when no visible target resolves.
    """
    vw, vh = _viewport_wh(page)
    margin = 8
    if targets:
        # _pick_visible_locator already restricts to in-viewport elements, so the
        # box below is guaranteed to be on-screen; the clamp is belt-and-braces.
        loc = _pick_visible_locator(page, rng, ", ".join(targets))
        if loc is not None:
            try:
                box = loc.bounding_box()
                if box and box.get("width") and box.get("height"):
                    cx = int(box["x"] + box["width"] * rng.uniform(0.3, 0.7))
                    cy = int(box["y"] + box["height"] * rng.uniform(0.25, 0.6))
                    cx = max(margin, min(vw - margin, cx))
                    cy = max(margin, min(vh - margin, cy))
                    return cx, cy
            except Exception:
                pass
    return rng.randint(margin, max(margin + 1, vw - margin)), rng.randint(margin, max(margin + 1, vh - margin))


def run_markov_simulation(
    page: Any,
    behavior_plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
    max_steps: int = 25,
    initial_state: str | None = None,
    initial_intent: str | None = None,
    intent_steps_left: int | None = None,
    suppress_intro_wait: bool = False,
    mousemove_targets: tuple[str, ...] | None = None,
    hover_targets: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Execute a dynamic, semi-Markov-driven browsing session.

    ``initial_state`` / ``initial_intent`` / ``intent_steps_left`` let a caller
    CONTINUE a prior walk across multiple short bursts instead of restarting at
    ``idle``/``reading`` each time (the return value carries the continuation
    state). ``suppress_intro_wait`` skips the fixed ~1s settle so chained bursts
    don't stamp a periodic preamble signature. ``mousemove_targets`` /
    ``hover_targets`` aim the random walk's moves/hovers at real on-page content
    (e.g. feed posts) instead of arbitrary viewport coordinates."""
    plan = behavior_plan or {}
    recorder = recorder or SessionRecorder(plan)

    # Single advancing per-session RNG (see interaction_primitives). By DEFAULT this
    # is fresh OS entropy so every run produces a different, non-repeating sequence;
    # an explicit plan ``session_seed`` reproduces the whole walk for debugging.
    rng = _session_rng(page, plan)

    # Load transition matrix from the plan if provided (e.g. custom user upload),
    # otherwise use the hand-tuned default. A provided matrix is validated and
    # row-normalized up front; bad rows are reported, not silently swapped out.
    custom_matrix = plan.get("markov_matrix") or plan.get("variations", {}).get("markov_matrix")
    if custom_matrix:
        matrix = _normalize_matrix(custom_matrix, source="custom")
        logger.info("[markov_simulator] using custom transition matrix with %d rows", len(matrix))
    else:
        matrix = DEFAULT_HUMAN_MATRIX

    if not suppress_intro_wait:
        update_status_ticker(page, "🎲 SEMI-MARKOV INITIALIZED", "Generating probabilistic pathing...")
        wait_human(page, plan, 0, 1000, recorder=recorder)

    # Continue a prior walk when a caller threads state across bursts, else start
    # fresh at idle/reading.
    current_state = initial_state if initial_state in matrix else "idle"
    steps_executed = 0
    history = [current_state]

    # Higher-level intent (goal) layer: a semi-Markov chain over intents, each with a
    # sampled sojourn in sub-steps. The active intent biases the low-level row.
    if initial_intent and initial_intent in INTENT_TRANSITIONS:
        intent = initial_intent
    else:
        intent = _choose_intent(rng, "reading")
    intent_history = [intent]
    remaining = intent_steps_left if (intent_steps_left and intent_steps_left > 0) else max(
        1, int(lognormal_ms(rng, mean_ms=INTENT_SOJOURN_STEPS[intent][0],
                            cv=INTENT_SOJOURN_STEPS[intent][1], lo=1, hi=12))
    )

    while steps_executed < max_steps:
        steps_executed += 1

        # Higher-level transition: when the current intent's sojourn elapses, pick a
        # new intent (with stickiness) and resample its sub-step budget.
        if remaining <= 0:
            intent = _choose_intent(rng, intent)
            intent_history.append(intent)
            remaining = max(1, int(lognormal_ms(rng, mean_ms=INTENT_SOJOURN_STEPS[intent][0],
                                                cv=INTENT_SOJOURN_STEPS[intent][1], lo=1, hi=12)))
            update_status_ticker(page, "🎯 INTENT", f"Switching focus -> {intent}")
        remaining -= 1

        # 1. Choose next state from the intent-biased current transition row.
        probs = matrix.get(current_state, DEFAULT_HUMAN_MATRIX["idle"])
        if not probs or sum(probs.values()) <= 0:
            # An all-zero row is a real defect; report it rather than guessing.
            raise ValueError(f"transition row for '{current_state}' has non-positive weight sum")
        states, weights = _biased_row(probs, intent)

        next_state = rng.choices(states, weights=weights, k=1)[0]

        logger.info("[markov_simulator] Step %d [%s]: %s -> %s", steps_executed, intent, current_state, next_state)
        history.append(next_state)

        # 2. Sample the explicit per-state sojourn time for this visit. Reading/idle
        #    dwell is correlated with the amount of visible content on screen.
        dwell_ms = _sample_dwell_ms(rng, next_state)
        if next_state in ("idle", "scroll_down", "scroll_up"):
            dwell_ms = int(dwell_ms * _reading_scale(_visible_text_amount(page)))
            dwell_ms = max(DWELL_LO_MS, min(DWELL_HI_MS, dwell_ms))

        # 3. Execute the interaction primitive corresponding to the next state.
        traced = False
        try:
            if next_state == "idle":
                # Reading: sometimes the reader traces/underlines a sentence with
                # the cursor (an active engaged-reading gesture) rather than just
                # holding still. The trace itself consumes part of the read time,
                # so we shorten the trailing dwell when it fires.
                if rng.random() < 0.35:
                    traced = trace_text_selection(page, plan, recorder=recorder)
                if traced:
                    update_status_ticker(page, "✍️ READING", "Tracing a sentence while reading...")
                    dwell_ms = int(dwell_ms * rng.uniform(0.2, 0.6))
                else:
                    update_status_ticker(page, "👁️ READING", f"Pausing to read content ({dwell_ms}ms)...")

            elif next_state == "mousemove":
                cx, cy = _content_move_target(page, rng, mousemove_targets)
                update_status_ticker(page, "🖱️ MOVING", f"Moving mouse to ({cx}, {cy})...")
                move_pointer(page, cx, cy, plan, recorder=recorder)

            elif next_state == "scroll_down":
                # A human on the wheel does a RUN of flicks before repositioning the
                # hand — not the metronomic scroll/move/scroll/move alternation. So a
                # single scroll_down state fires 1-5 small flicks, and BETWEEN flicks
                # the hand is usually still but sometimes nudges the cursor a few px
                # (the "resting on the mouse while wheeling" jitter). The full
                # repositioning move only happens on the next mousemove transition.
                n_flicks = rng.randint(1, 5)
                for fi in range(n_flicks):
                    delta = rng.randint(200, 480)
                    update_status_ticker(
                        page, "📜 SCROLLING", f"Scrolling down ~{delta}px ({fi + 1}/{n_flicks})..."
                    )
                    scroll_page(page, plan, pass_index=steps_executed + fi, fallback_delta_y=delta, recorder=recorder)
                    if fi < n_flicks - 1:
                        # The short beat between wheel flicks.
                        page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=170.0, cv=0.5, lo=40.0, hi=650.0)))
                        # Mostly still; ~35% of the time a tiny nudge (a few px), with
                        # a slight downward bias as the eye follows the text.
                        if rng.random() < 0.35:
                            cx, cy = _get_cursor(page)
                            move_pointer(
                                page,
                                cx + rng.uniform(-7, 7),
                                cy + rng.uniform(-4, 10),
                                plan,
                                recorder=recorder,
                            )

            elif next_state == "scroll_up":
                # Negative signed delta: honored downstream by planned_scroll_delta
                # so the wheel actually scrolls UP for a re-read.
                delta = -rng.randint(150, 500)
                update_status_ticker(page, "📜 SCROLLING", f"Scrolling up ~{-delta}px (re-reading)...")
                scroll_page(page, plan, pass_index=steps_executed, fallback_delta_y=delta, recorder=recorder)

            elif next_state == "hover":
                update_status_ticker(page, "👁️ HOVERING", "Looking for hover target...")
                hover_sel = ", ".join(hover_targets) if hover_targets else "a[href], button, input"
                target = _pick_visible_locator(page, rng, hover_sel)
                if target is not None:
                    hover_element(page, target, plan, recorder=recorder)
                else:
                    logger.info("[markov_simulator] no visible hover target; skipping hover step")

            elif next_state == "click":
                update_status_ticker(page, "🖱️ CLICKING", "Choosing element to click...")
                target = _pick_visible_locator(page, rng, "a[href], button")
                if target is not None:
                    try:
                        url_before = page.url
                    except Exception:
                        url_before = None
                    click_element(page, target, plan, recorder=recorder)
                    # If the click navigated to a new view, perceive it before acting.
                    try:
                        navigated = url_before is not None and page.url != url_before
                    except Exception:
                        navigated = False
                    if navigated:
                        perceive_after_render(page, plan, recorder=recorder)
                else:
                    logger.info("[markov_simulator] no visible click target; skipping click step")

            elif next_state == "typing":
                inputs = page.locator("input[type=text], textarea, input[type=search]").all()
                visible_inputs = [i for i in inputs if i.is_visible()]
                if visible_inputs:
                    target_input = rng.choice(visible_inputs)
                    text = rng.choice(TYPING_QUERIES)
                    update_status_ticker(page, "⌨️ TYPING", f"Typing query: '{text}'")
                    type_text(page, target_input, text, plan, recorder=recorder)
                else:
                    # No input present: a mouse move is the honest no-op for this
                    # state, not a fabricated typing success.
                    cx, cy = _content_move_target(page, rng, mousemove_targets)
                    move_pointer(page, cx, cy, plan, recorder=recorder)

        except Exception as e:
            logger.warning("[markov_simulator] Error during state execution (%s): %s", next_state, e)

        # 4. Semi-Markov sojourn. Dwell models WAITING / reading, not a mandatory
        #    pause after every action. When actively doing things (scroll, move,
        #    hover in a flow) a human frequently chains straight into the next
        #    action with little or no pause; only idle/reading holds the full
        #    sojourn. Collapsing most active-state dwells gives the "scroll, move,
        #    scroll, move" rhythm and breaks the uniform act-pause-act-pause cadence.
        if next_state != "idle" and not traced and rng.random() < 0.55:
            dwell_ms = int(dwell_ms * rng.uniform(0.04, 0.3))
        if dwell_ms > 0:
            page.wait_for_timeout(dwell_ms)
        if recorder is not None:
            recorder.record("dwell", metadata={"state": next_state, "dwell_ms": dwell_ms})

        current_state = next_state

        # 5. Short inter-step transition gap (right-skewed log-normal, not uniform).
        gap_ms = int(
            lognormal_ms(rng, mean_ms=INTER_STEP_MEAN_MS, cv=INTER_STEP_CV, lo=DWELL_LO_MS, hi=GAP_HI_MS)
        )
        page.wait_for_timeout(gap_ms)

        # 6. Occasional distraction: a rare, heavy-tailed pause where the operator
        #    briefly looks away. Modelled by a Weibull (shape < 1 => heavy tail).
        if rng.random() < DISTRACTION_CHANCE:
            distract_ms = int(
                weibull_ms(rng, scale_ms=DISTRACTION_SCALE_MS, shape=DISTRACTION_SHAPE, lo=400.0, hi=DISTRACTION_HI_MS)
            )
            update_status_ticker(page, "💤 DISTRACTED", f"Brief attention lapse ({distract_ms}ms)...")
            page.wait_for_timeout(distract_ms)
            if recorder is not None:
                recorder.record("distraction", metadata={"distract_ms": distract_ms})

    if not suppress_intro_wait:
        update_status_ticker(page, "🏁 COMPLETED", "Semi-Markov simulation finished.")
    return {
        "steps_executed": steps_executed,
        "state_history": history,
        "intent_history": intent_history,
        "final_state": current_state,
        "final_intent": intent,
        "intent_steps_left": remaining,
    }
