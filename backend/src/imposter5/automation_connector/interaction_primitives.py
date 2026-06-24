"""Small browser interaction primitives for automation connector runs.

These provide the high-level targets and sequences (move to coords with style, position-then-wheel, hover+read, etc.).
The actual human curves/wobbles/bursts/velocities come from the Cloak humanize layer (see cloak_runtime + behavior plan pointer section).
Use move_pointer / scroll_page etc (or _safe_mouse_move for robustness); never raw page.mouse.* in new code so that plan variance + recording + styled physics are always applied.
"""
from __future__ import annotations

import logging
import math
import os
import random
import string
import time
from typing import Any

from imposter5.automation_connector.behavior_policy import (
    planned_scroll_delta,
    planned_wait_ms,
)
from imposter5.automation_connector.humanize_dist import (
    fitts_movement_time_ms,
    lognormal_ms,
    min_jerk_progress,
    scroll_decay_deltas,
    two_thirds_power_dwell_scale,
)
from imposter5.automation_connector.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)


def _seeded_rng(plan: dict[str, Any] | None, namespace: str) -> random.Random:
    """Legacy per-namespace deterministic RNG (kept for backward compatibility).

    NOTE: new interactive code paths should use :func:`_session_rng`, the single
    advancing per-session stream, so that the same start/end never produces the same
    path. Re-seeding a fresh RNG from a fixed ``run_id:namespace`` made repeated
    actions byte-identical, which violates human trial-to-trial motor variability
    (Harris & Wolpert, 1998). This helper remains only for callers that explicitly
    want a reproducible side-channel draw.
    """
    seed = ""
    if isinstance(plan, dict):
        seed = str(plan.get("run_id") or "")
    return random.Random(f"{seed}:{namespace}")


# --- Single advancing per-session RNG + session state -----------------------------
#
# Human motor output is never repeated identically: trial-to-trial variability is a
# fundamental property of the motor system, driven by signal-dependent neuromotor
# noise (Harris & Wolpert, 1998, *Nature*; Faisal, Selen & Wolpert, 2008, *Nat. Rev.
# Neurosci.*). A real person who moves the cursor from A to B twice produces two
# different trajectories. To match that, ALL interactive randomness in a session is
# drawn from ONE advancing ``random.Random`` stored on the page (the session), so
# every draw is fresh. By DEFAULT the stream is seeded from fresh OS entropy, so
# sessions genuinely differ ("random like a human"); when the plan carries an
# explicit ``session_seed`` the entire session is reproducible for debugging.


def _session_state(page: Any, plan: dict[str, Any] | None) -> dict[str, Any]:
    """Return (creating if needed) the per-session state bag attached to the page.

    Holds the single advancing RNG, the session start time, and an action counter
    used for intra-session fatigue drift.
    """
    state = getattr(page, "_imposter_session", None)
    if isinstance(state, dict) and isinstance(state.get("rng"), random.Random):
        return state

    seed: Any = None
    if isinstance(plan, dict):
        seed = plan.get("session_seed")
    if seed is not None and str(seed) != "":
        rng = random.Random(str(seed))
        source = "seed"
    else:
        # Fresh per-session entropy: never the same twice by default.
        rng = random.Random(os.urandom(16))
        source = "entropy"

    state = {"rng": rng, "started": time.monotonic(), "actions": 0, "seed_source": source}
    try:
        page._imposter_session = state
    except Exception:
        logger.exception("[interaction_primitives] could not attach _imposter_session to page")
        # If the page rejects attributes (some mocks), stash on the plan dict so the
        # stream still advances within the session rather than silently re-seeding.
        if isinstance(plan, dict):
            plan["_imposter_session"] = state
    return state


def _session_rng(page: Any, plan: dict[str, Any] | None) -> random.Random:
    """The one advancing RNG for this session (see module note)."""
    return _session_state(page, plan)["rng"]


def _fatigue(page: Any, plan: dict[str, Any] | None) -> dict[str, float]:
    """Advance the action counter and return current intra-session drift factors.

    Real operators get slightly slower and sloppier over a long session
    (mental-fatigue / time-on-task effects degrade motor precision and slow
    responses; e.g. Boksem & Tops, 2008, *Brain Res. Rev.*). The drift is a smooth
    saturating curve in the number of actions taken: ``frac = n / (n + half)`` rises
    from 0 toward 1, reaching 0.5 at ``half_actions``. We return a ``slowdown``
    multiplier (>= 1, scales movement/keystroke time up) and a ``sloppiness``
    multiplier (>= 1, scales tremor amplitude and endpoint scatter up).
    """
    state = _session_state(page, plan)
    state["actions"] = int(state.get("actions", 0)) + 1
    n = float(state["actions"])

    cfg = plan.get("fatigue") if isinstance(plan, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("enabled", True)):
        return {"slowdown": 1.0, "sloppiness": 1.0}
    max_slow = _bounded_float(cfg.get("max_slowdown"), lower=0.0, upper=0.6, default=0.15)
    max_sloppy = _bounded_float(cfg.get("max_sloppiness"), lower=0.0, upper=0.8, default=0.20)
    half = float(_bounded_int(cfg.get("half_actions"), lower=5, upper=400, default=60))
    frac = n / (n + half)
    return {"slowdown": 1.0 + max_slow * frac, "sloppiness": 1.0 + max_sloppy * frac}


def _safe_mouse_move(page: Any, x: float, y: float, plan: dict[str, Any] | None, rng: random.Random | None = None, *, recorder: SessionRecorder | None = None) -> dict[str, Any]:
    """Preferred path for all mouse positioning: uses move_pointer (plan-driven style/imprecision/overshoot + recorder)
    so that even fallback paths in scroll positioning or reading hovers get the human-like trajectories.
    Falls back to raw mouse.move + a recorded raw event only if the styled path fails (e.g. during page transitions).
    This keeps movement quality high and consistent for detectors / human-twin traces.
    """
    try:
        return move_pointer(page, x, y, plan, recorder=recorder)
    except Exception:
        try:
            page.mouse.move(x, y)
        except Exception:
            pass
        meta = {"x": round(x), "y": round(y), "style": "raw_fallback"}
        if recorder is not None:
            recorder.record("mouse_move", metadata=meta)
        return meta


def _bounded_int(value: Any, *, lower: int, upper: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _bounded_float(value: Any, *, lower: float, upper: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _human_config(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Physics/humanization knobs live on the plan (not os.environ).

    Returns ``plan["human_config"]`` when present and a dict, else an empty dict so
    callers can use ``.get(..., default)`` for every tunable.
    """
    if isinstance(plan, dict):
        hc = plan.get("human_config", {})
        if isinstance(hc, dict):
            return hc
    return {}


def wait_human(
    page: Any,
    plan: dict[str, Any] | None,
    pass_index: int = 0,
    fallback_ms: int = 1_500,
    *,
    recorder: SessionRecorder | None = None,
) -> int:
    """Wait for a planned bounded interval and return the actual wait."""
    wait_ms = planned_wait_ms(plan, pass_index, fallback_ms)
    update_status_ticker(page, "⏳ WAITING / READING", f"Pause: {wait_ms}ms (pass {pass_index})")
    page.wait_for_timeout(wait_ms)
    if recorder is not None:
        recorder.record("wait", metadata={"pass_index": pass_index, "wait_ms": wait_ms})
    return wait_ms


def perceive_after_render(
    page: Any,
    plan: dict[str, Any] | None = None,
    *,
    scale: float = 1.0,
    recorder: SessionRecorder | None = None,
) -> int:
    """Pay a human perceive-decide-initiate latency after a view/content render.

    A sighted person must visually take in newly rendered content (saccade,
    fixation, comprehension) and decide before the hand starts moving; that
    latency lives in the hundreds of milliseconds and is never sub-~200 ms.
    Call this after a navigation / view change and before the first interaction
    on the new view so the first action reads as a genuine reaction rather than
    an instantaneous (physiologically impossible) one. Drawn from the shared
    log-normal timing primitive on the single advancing per-session RNG, with an
    absolute physiological floor. The floor is grounded in human-factors
    reaction time, not on any detector threshold.

    ``scale`` compresses the variable part (test-only fast paths); the floor is
    absolute and never compresses below it.
    """
    rng = _session_rng(page, plan)
    base = lognormal_ms(rng, mean_ms=450.0, cv=0.4, lo=250.0, hi=1600.0)
    ms = int(max(250.0, base * max(float(scale), 0.25)))
    try:
        page.wait_for_timeout(ms)
    except Exception:
        logger.debug("[interaction_primitives] perceive_after_render wait failed", exc_info=True)
    if recorder is not None:
        try:
            recorder.record("perceive_after_render", metadata={"latency_ms": ms})
        except Exception:
            logger.debug("[interaction_primitives] perceive_after_render record failed", exc_info=True)
    return ms


def scroll_page(
    page: Any,
    plan: dict[str, Any] | None,
    pass_index: int = 0,
    fallback_delta_y: int = 900,
    *,
    recorder: SessionRecorder | None = None,
    honor_fallback: bool = False,
) -> int:
    """Scroll using a planned bounded delta, with mouse positioning over content first so the wheel is accompanied by realistic mouse events (for detectors and human twin traces). Returns the actual delta used.

    ``honor_fallback`` makes the call use ``fallback_delta_y`` verbatim (sign and
    magnitude) instead of the plan's precomputed pacing list. The semi-Markov feed
    engine sets this: it samples a fresh, randomized per-flick magnitude on every
    wheel flick and owns scroll variety itself. Routing those flicks through the
    pacing list was the scroll regression — the list is indexed by an ever-growing
    step counter, so ``planned_scroll_delta`` clamped to the list's LAST element and
    made every feed scroll an identical magnitude (the "no scroll randomness" tell).
    The legacy per-pass loop keeps the default (read the pacing list)."""
    if honor_fallback:
        delta_y = int(fallback_delta_y)
    else:
        delta_y = planned_scroll_delta(plan, pass_index, fallback_delta_y)

    rng = _session_rng(page, plan)
    update_status_ticker(page, "📜 SCROLLING", f"Delta: {delta_y}px (pass {pass_index})")
    # Position the mouse over a content area before the wheel so the scroll "event" has an associated cursor position (mouse scroll realism, not just raw wheel from nowhere).
    _position_mouse_over_content(page, plan, rng, recorder)

    # Real scroll-momentum decay: one big wheel burst that geometrically bleeds off,
    # emitting decreasing wheel events with short waits + a final settle pause.
    # Sign is honored (scroll-up == negative delta_y => all-negative steps).
    hc = _human_config(plan)
    max_steps = _bounded_int(hc.get("scroll_max_steps"), lower=1, upper=20, default=8)
    decay = _bounded_float(hc.get("scroll_decay"), lower=0.2, upper=0.95, default=0.6)
    step_mean = _bounded_float(hc.get("scroll_step_pause_ms"), lower=4.0, upper=400.0, default=55.0)
    step_cv = _bounded_float(hc.get("scroll_step_pause_cv"), lower=0.0, upper=2.0, default=0.4)
    settle_mean = _bounded_float(hc.get("scroll_settle_ms"), lower=20.0, upper=1500.0, default=220.0)
    overshoot_chance = _bounded_float(hc.get("scroll_overshoot_chance"), lower=0.0, upper=0.6, default=0.22)

    deltas = scroll_decay_deltas(rng, total_px=float(delta_y), max_steps=max_steps, decay=decay)
    for step_delta in deltas:
        page.mouse.wheel(0, step_delta)
        page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=step_mean, cv=step_cv, lo=4.0, hi=400.0)))

    # Over-scroll correction (the missing reverse-direction "debounce"). Momentum
    # decay alone never reverses, so every burst stopped cleanly in one direction —
    # a tell. A human routinely flicks a touch past where they meant to stop and
    # nudges back the OTHER way once the eyes catch the content. Fire occasionally
    # with a brief "too far" reaction pause before the corrective nudge.
    overshoot_px = 0
    if deltas and rng.random() < overshoot_chance:
        sign = -1 if delta_y >= 0 else 1
        overshoot_px = sign * int(max(18, min(90, abs(delta_y) * rng.uniform(0.05, 0.16))))
        page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=140.0, cv=0.4, lo=60.0, hi=320.0)))
        page.mouse.wheel(0, overshoot_px)

    # Settle pause (eyes catch up to the content after the flick stops).
    page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=settle_mean, cv=0.4, lo=20.0, hi=1500.0)))

    # Small post-scroll mouse adjustment (simulates eyes/hand following the content after scroll).
    if rng.random() < 0.4:
        _micro_mouse_adjust(page, plan, rng, recorder)
    if recorder is not None:
        recorder.record(
            "scroll",
            metadata={
                "pass_index": pass_index, "delta_y": delta_y,
                "steps": len(deltas), "overshoot_px": overshoot_px,
            },
        )
    return delta_y


def _position_mouse_over_content(page: Any, plan: dict[str, Any] | None, rng: random.Random, recorder: SessionRecorder | None = None) -> None:
    """Move the mouse over a VISIBLE content spot before scrolling.

    This must land the cursor inside the current viewport, not merely inside the
    first matching element's box. A content container (e.g. the first feed
    ``article``) is frequently scrolled ABOVE the viewport after the first
    scroll; aiming at its box then puts the cursor off-screen. Chromium still
    scrolls the page from an off-screen cursor via the compositor, but it does
    NOT dispatch a DOM ``wheel`` event — so every wheel-based detector (and the
    Blue gauntlet's scroll-kinetics scorer) sees a session with zero scrolling
    even though the page moved. Anchoring to a visible point keeps the cursor
    over real content (more human, not less) and guarantees the wheel dispatches.

    The visible target is computed in one JS pass (fast even on a 500-element
    feed) by intersecting candidate containers with the viewport; a clamped
    viewport-center point is the last resort so the wheel always has a real
    on-screen origin.
    """
    try:
        spot = _safe_evaluate(
            page,
            """() => {
                const vw = window.innerWidth, vh = window.innerHeight;
                const HEADER = 64;  // keep below a typical fixed app bar
                const sels = ['article', 'main', "[role='main']",
                              "section[aria-label*='feed' i]", '.g-feed-post'];
                const rects = [];
                for (const s of sels) {
                    const els = document.querySelectorAll(s);
                    for (let i = 0; i < Math.min(els.length, 40); i++) {
                        const r = els[i].getBoundingClientRect();
                        const top = Math.max(r.top, HEADER), bot = Math.min(r.bottom, vh - 8);
                        const left = Math.max(r.left, 8), right = Math.min(r.right, vw - 8);
                        if (bot - top > 40 && right - left > 60) {
                            rects.push({left, top, w: right - left, h: bot - top});
                            if (rects.length >= 8) break;
                        }
                    }
                    if (rects.length >= 8) break;
                }
                return {rects, vw, vh, header: HEADER};
            }""",
        )
    except Exception:
        spot = None
    if not isinstance(spot, dict):
        return
    vw = float(spot.get("vw") or 0.0)
    vh = float(spot.get("vh") or 0.0)
    rects = spot.get("rects") if isinstance(spot.get("rects"), list) else []
    header = float(spot.get("header") or 64.0)
    try:
        # A reader does NOT hold the pointer pinned to the dead-centre of the
        # content column flick after flick. Vary WHERE the wheel originates: most
        # of the time over a (randomly chosen) on-screen post with a wide spread
        # inside it, but a meaningful share of the time parked off toward a rail /
        # gutter. The cursor stays on-screen either way so the DOM wheel event
        # always dispatches (an off-screen cursor silently drops the wheel event).
        roll = rng.random()
        if vw > 0 and vh > 0 and roll < 0.30:
            # Off-column rest: left rail or right gutter, anywhere down the page.
            if rng.random() < 0.5:
                x = vw * rng.uniform(0.04, 0.17)
            else:
                x = vw * rng.uniform(0.83, 0.96)
            y = header + (vh - header - 8) * rng.uniform(0.12, 0.88)
        elif rects:
            r = rects[rng.randrange(len(rects))]
            x = r["left"] + r["w"] * rng.uniform(0.14, 0.86)
            y = r["top"] + r["h"] * rng.uniform(0.18, 0.84)
        elif vw > 0 and vh > 0:
            # No resolvable content: a varied viewport point (never fixed centre).
            x = vw * rng.uniform(0.18, 0.82)
            y = header + (vh - header - 8) * rng.uniform(0.15, 0.85)
        else:
            return
        _safe_mouse_move(page, x, y, plan, rng, recorder=recorder)
    except Exception:
        pass


def _micro_mouse_adjust(page: Any, plan: dict[str, Any] | None, rng: random.Random, recorder: SessionRecorder | None = None) -> None:
    """Tiny RELATIVE settle nudge after a scroll (eyes/hand follow the content).

    Nudges a few pixels from wherever the cursor already is — never a jump to a
    fixed coordinate, which looked like the cursor teleporting then creeping.
    """
    try:
        cx, cy = _get_cursor(page)
        nx = cx + rng.uniform(-7, 7)
        ny = cy + rng.uniform(-7, 7)
        _safe_mouse_move(page, nx, ny, plan, rng, recorder=recorder)
    except Exception:
        pass


def scroll_feed_flicks(
    page: Any,
    plan: dict[str, Any] | None,
    pass_index: int = 0,
    *,
    recorder: SessionRecorder | None = None,
    min_flicks: int = 1,
    max_flicks: int = 5,
) -> dict[str, Any]:
    """Scroll the feed as a *run* of 1-5 wheel flicks before the hand repositions.

    A person on a scroll wheel fires several quick flicks in a row, with the cursor
    usually resting still between them (occasionally nudging a few px), and only
    repositions on a separate beat. One scroll + one full mouse move per pass (the
    old rhythm) reads as metronomic — this is the tell it fixes. The mouse is
    positioned over content once at the start of the burst; flicks then fire
    without a full reposition between them.
    """
    rng = _seeded_rng(plan, f"flicks:{pass_index}")
    base = planned_scroll_delta(plan, pass_index, 900)
    n_flicks = rng.randint(max(1, min_flicks), max(max(1, min_flicks), max_flicks))
    _position_mouse_over_content(page, plan, rng, recorder)
    total = 0
    for fi in range(n_flicks):
        delta = max(140, min(520, int(base * rng.uniform(0.28, 0.62))))
        try:
            page.mouse.wheel(0, delta)
        except Exception:
            pass
        total += delta
        if recorder is not None:
            recorder.record("scroll", metadata={"pass_index": pass_index, "delta_y": delta, "flick": fi})
        if fi < n_flicks - 1:
            page.wait_for_timeout(rng.randint(45, 360))
            if rng.random() < 0.35:
                _micro_mouse_adjust(page, plan, rng, recorder)
    return {"flicks": n_flicks, "delta_y": total}


def _neighboring_char(char: str) -> str:
    alphabet = string.ascii_lowercase
    lower = char.lower()
    if lower not in alphabet:
        return char
    idx = alphabet.index(lower)
    # Pick an adjacent letter; for the last letter ('z') step back instead of
    # clamping to itself (which produced a no-op "typo").
    neighbor_idx = idx - 1 if idx >= len(alphabet) - 1 else idx + 1
    replacement = alphabet[neighbor_idx]
    return replacement.upper() if char.isupper() else replacement


# --- Stateful honeypot detection (Layer 4 defense) -------------------------------
#
# Honeypots are real DOM nodes that are styled invisible/unreachable to humans but
# present to naive automation. We must evaluate the trap test on the SAME element
# that will be clicked/hovered (resolve the locator's .first / an element handle and
# run the check on THAT element), never on a fresh ``document.querySelector`` first
# match — a strict-mode selector can resolve to several elements and the first DOM
# match is frequently not the one Playwright would act on.
_HONEYPOT_CHECK_JS = r"""
(el) => {
    if (!el) return "";
    const reasons = [];
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();

    if (style.display === 'none') reasons.push('display:none');
    if (style.visibility === 'hidden' || style.visibility === 'collapse') reasons.push('visibility:hidden');
    if (parseFloat(style.opacity) <= 0.01) reasons.push('opacity~0');

    // Off-screen positioning (fully outside the viewport in any direction).
    if (rect.right < 0 || rect.bottom < 0 || rect.left > window.innerWidth || rect.top > window.innerHeight) {
        reasons.push('offscreen');
    }
    if (rect.width === 0 || rect.height === 0) reasons.push('zero-size');
    // ~1px sizing (classic hidden honeypot input).
    else if (rect.width <= 1 && rect.height <= 1) reasons.push('1px-size');

    if (parseFloat(style.fontSize) === 0) reasons.push('fontSize:0');

    const z = parseInt(style.zIndex, 10);
    if (!isNaN(z) && z < -1000) reasons.push('zIndex<-1000');

    if ((el.getAttribute('aria-hidden') || '') === 'true') reasons.push('aria-hidden');
    if ((el.getAttribute('tabindex') || '') === '-1') reasons.push('tabindex=-1');

    const clip = (style.clip || '').replace(/\s+/g, '');
    const clipPath = (style.clipPath || '').replace(/\s+/g, '');
    if (clip === 'rect(0px,0px,0px,0px)' || clipPath.indexOf('inset(100%') !== -1) reasons.push('clip-inset');

    const ti = parseFloat(style.textIndent);
    if (!isNaN(ti) && ti <= -1000) reasons.push('text-indent');

    return reasons.join(',');
}
"""


def _honeypot_state(page: Any) -> dict[str, Any]:
    """Per-page honeypot memory, persisted on the page object across calls."""
    state = getattr(page, "_honeypot_state", None)
    if not isinstance(state, dict) or "seen" not in state:
        state = {"seen": set(), "count": 0}
        try:
            page._honeypot_state = state
        except Exception:
            logger.exception("[interaction_primitives] could not attach _honeypot_state to page")
    return state


def _detect_honeypot_reason(target: Any) -> str:
    """Run the trap test on the concrete element the caller will act on.

    ``target`` is a Playwright Locator (we narrow to ``.first``) or an ElementHandle
    (used directly). Returns a comma-joined reason string when the element is a
    honeypot, otherwise an empty string. Detection failures are logged (not silently
    swallowed) and treated as "not a honeypot" so a flaky check never blocks a real
    interaction.
    """
    try:
        if hasattr(target, "first"):  # Locator
            return target.first.evaluate(_HONEYPOT_CHECK_JS) or ""
        return target.evaluate(_HONEYPOT_CHECK_JS) or ""
    except Exception:
        logger.exception("[interaction_primitives] honeypot detection evaluate failed")
        return ""


def _register_honeypot(page: Any, label: str, reason: str, recorder: SessionRecorder | None) -> dict[str, Any]:
    """Record a detected trap into per-page state and the session recorder."""
    state = _honeypot_state(page)
    signature = f"{label}|{reason}"
    state["seen"].add(signature)
    state["count"] = int(state.get("count", 0)) + 1
    update_status_ticker(page, "⚠️ EVADING HONEYPOT", f"Trap ({reason}) on {label}; bypassing.")
    meta = {
        "selector": label,
        "reason": reason,
        "total_evaded": state["count"],
        "unique_traps": len(state["seen"]),
    }
    if recorder is not None:
        recorder.record("honeypot_evaded", metadata=meta)
    return meta


# QWERTY hand/finger map for digraph-dependent keystroke timing. Inter-key latency
# in human typing depends strongly on the key PAIR, not a flat per-key delay:
# alternating-hand digraphs are faster than same-hand ones, and same-FINGER digraphs
# are the slowest (Dhakal et al., 2018, CHI, "Observations on Typing from 136M
# Keystrokes"; Gentner, 1983). We approximate this with a coarse layout map.
_QWERTY_LEFT = set("qwertasdfgzxcvb12345")
_QWERTY_RIGHT = set("yuiophjklnm67890")
_QWERTY_FINGER: dict[str, int] = {}
for _row in ("qwertyuiop", "asdfghjkl", "zxcvbnm"):
    for _i, _ch in enumerate(_row):
        # Coarse finger index 0..9 by column; good enough to flag same-finger pairs.
        _QWERTY_FINGER[_ch] = _i


def _digraph_latency_factor(prev: str, cur: str) -> float:
    """Multiplier on the base inter-key latency for the (prev -> cur) digraph.

    Reflects well-replicated typing-dynamics effects: repeated keys and
    alternating-hand pairs are quick; same-hand pairs are slower; same-finger pairs
    are slowest; a space/word boundary adds a small planning pause.
    """
    if not prev:
        return 1.0
    p, c = prev.lower(), cur.lower()
    if c == " " or p == " ":
        return 1.35  # word-boundary planning pause
    if p == c:
        return 0.80  # key repeat (e.g. "ll")
    p_left, c_left = p in _QWERTY_LEFT, c in _QWERTY_LEFT
    p_right, c_right = p in _QWERTY_RIGHT, c in _QWERTY_RIGHT
    if (p_left and c_right) or (p_right and c_left):
        return 0.82  # alternating hands: fast
    if p in _QWERTY_FINGER and c in _QWERTY_FINGER and _QWERTY_FINGER[p] == _QWERTY_FINGER[c]:
        return 1.55  # same finger, different key: slow/awkward
    return 1.05  # same hand, different finger


def type_text(
    page: Any,
    selector: Any,
    text: str,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Type text with digraph-dependent rhythm, key-hold dwell, and corrected typos.

    ``selector`` may be a string selector OR an already-resolved Locator / element
    handle (matching ``click_element``'s contract).

    Realism model (digraph mode — modern plans carrying ``base_interkey_ms``):
    - REAL key events. Each keystroke is a ``page.keyboard.press(key, delay=hold)``
      (keydown -> held -> keyup), so the press-to-release DWELL is genuine. Bare
      ``locator.type()`` reports ~0 ms constant dwell, which is the keystroke-dynamics
      dead giveaway; we drive the keyboard directly instead.
    - Rhythm from measured typing dynamics: the keydown->keydown interval (IKI) is a
      per-digraph multiplier (:func:`_digraph_latency_factor`) on the typist's base
      latency, log-normal so it is right-skewed (occasional hesitations). The IKI
      already includes the previous key's hold, so flight = ``IKI - prev_hold`` (no
      sluggish double-count), plus occasional planning pauses.
    - Varied mistakes (Dhakal et al., 2018): substitutions (neighbour key),
      doublings, insertions, and transpositions — usually noticed (after a reaction
      pause, sometimes a char or two late) and fixed with backspaces + retype, rarely
      left in. Error rate and latency both grow with intra-session fatigue.
    LEGACY mode (only ``min_delay_ms``/``max_delay_ms``) keeps the original single
    log-normal ``locator.type`` keystroke delay. All draws come from the single
    advancing per-session RNG.
    """
    label = selector if isinstance(selector, str) else "element_handle"
    locator = page.locator(selector) if isinstance(selector, str) else selector

    update_status_ticker(page, "⌨️ TYPING", f"Input: {label} - '{text[:20]}...'")
    typing_plan = plan.get("typing") if isinstance(plan, dict) else {}
    typing_plan = typing_plan if isinstance(typing_plan, dict) else {}

    rng = _session_rng(page, plan)
    drift = _fatigue(page, plan)

    # Two timing modes for backward compatibility:
    # - DIGRAPH mode (modern plans carry ``base_interkey_ms`` / ``key_hold_ms``):
    #   digraph-dependent inter-key gaps + per-key hold dwell + fatigue.
    # - LEGACY mode (only ``min_delay_ms``/``max_delay_ms`` present): a single
    #   log-normal keystroke ``delay`` drawn from that window, no separate inter-key
    #   gap. Preserves the original contract for callers that pin those knobs.
    use_digraph = "base_interkey_ms" in typing_plan
    use_hold = "key_hold_ms" in typing_plan

    min_delay = float(typing_plan.get("min_delay_ms", 55))
    max_delay = max(min_delay, float(typing_plan.get("max_delay_ms", 170)))
    legacy_mid = (min_delay + max_delay) / 2.0
    base_interkey = float(typing_plan.get("base_interkey_ms", legacy_mid)) * drift["slowdown"]
    interkey_cv = float(typing_plan.get("interkey_cv", 0.4))
    key_hold = float(typing_plan.get("key_hold_ms", legacy_mid))
    typo_chance = float(typing_plan.get("typo_chance", 0.0)) * drift["sloppiness"]
    correction_chance = float(typing_plan.get("correction_chance", 1.0))
    pause_chance = float(typing_plan.get("pause_mid_query_chance", 0.0))

    def _dwell_ms() -> int:
        """Key-hold (press->release) dwell for one keystroke, in ms."""
        mean = key_hold if use_hold else legacy_mid
        return int(lognormal_ms(rng, mean_ms=mean, cv=0.30, lo=18.0, hi=240.0))

    def _iki_ms(prev: str, cur: str) -> float:
        """Keydown->keydown interval for the (prev -> cur) digraph, in ms."""
        mean = base_interkey * _digraph_latency_factor(prev, cur)
        return lognormal_ms(rng, mean_ms=mean, cv=interkey_cv, lo=18.0, hi=1200.0)

    typed = 0
    typos = 0
    corrections = 0
    locator.click()  # focus the field as a human does before typing

    if not use_digraph:
        # LEGACY mode: a single log-normal keystroke ``delay`` from the [min, max]
        # window with no separate inter-key gap. Kept verbatim so callers/tests that
        # pin ``min_delay_ms``/``max_delay_ms`` keep their exact contract.
        def _press_delay() -> int:
            return int(lognormal_ms(rng, mean_ms=legacy_mid, cv=0.35, lo=min_delay, hi=max_delay))

        for index, char in enumerate(text):
            if rng.random() < pause_chance and index > 0:
                page.wait_for_timeout(rng.randint(250, 900))
            type_correct = True
            if typo_chance > 0 and char.strip() and rng.random() < typo_chance:
                locator.type(_neighboring_char(char), delay=_press_delay())
                typos += 1
                if rng.random() < correction_chance:
                    locator.press("Backspace")
                    corrections += 1
                else:
                    type_correct = False
            if type_correct:
                locator.type(char, delay=_press_delay())
            typed += 1

        result = {"typed_chars": typed, "typos": typos, "corrections": corrections}
        if recorder is not None:
            recorder.record("type_text", metadata={"selector": label, **result})
        return result

    # DIGRAPH mode: drive REAL key events (keydown -> hold -> keyup) through
    # ``page.keyboard`` so the press->release dwell is genuine, not a 0 ms ``type()``
    # insert (constant near-zero dwell is the keystroke-dynamics dead giveaway). The
    # keydown->keydown interval (IKI) carries the digraph rhythm and ALREADY INCLUDES
    # the previous key's hold, so flight (keyup -> next keydown) is ``IKI - prev_hold``
    # — this removes the old double-count (inter-key wait PLUS a second ``type`` delay)
    # that made search typing feel sluggish, while staying faithful to measured rates.
    kb = getattr(page, "keyboard", None)
    MIN_FLIGHT = 8.0
    state = {"prev": "", "last_hold": 0.0}

    def _press_key(key: str, hold_ms: int) -> None:
        """One real keystroke with a measurable press->release dwell."""
        k = "Space" if key == " " else key
        if kb is not None:
            try:
                kb.press(k, delay=int(hold_ms))
                return
            except Exception:
                pass
        # Fallback (no keyboard surface / unmappable key): keep the char correct.
        try:
            locator.type(key, delay=0)
        except Exception:
            pass

    def _stroke(ch: str) -> None:
        """Emit a content keystroke: digraph-paced flight, then a held key press."""
        flight = max(MIN_FLIGHT, _iki_ms(state["prev"], ch) - state["last_hold"])
        page.wait_for_timeout(int(flight))
        hold = _dwell_ms()
        _press_key(ch, hold)
        state["prev"] = ch
        state["last_hold"] = hold

    def _control(key: str) -> None:
        """Emit a control keystroke (Backspace) at a quick repeated-key cadence."""
        flight = max(
            MIN_FLIGHT,
            lognormal_ms(rng, mean_ms=base_interkey * 0.65, cv=0.30, lo=15.0, hi=500.0) - state["last_hold"],
        )
        page.wait_for_timeout(int(flight))
        hold = _dwell_ms()
        _press_key(key, hold)
        state["prev"] = ""  # a control key breaks the digraph chain
        state["last_hold"] = hold

    def _react() -> None:
        """Perceive-the-error reaction pause before correcting (~120-520 ms)."""
        page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=230.0, cv=0.45, lo=120.0, hi=520.0)))

    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # Occasional mid-query planning pause (deciding what to type next).
        if pause_chance and i > 0 and rng.random() < pause_chance:
            page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=520.0, cv=0.5, lo=240.0, hi=1500.0)))

        if not (typo_chance > 0 and char.strip() and rng.random() < typo_chance):
            _stroke(char)
            typed += 1
            i += 1
            continue

        # --- a mistake happens ------------------------------------------------------
        typos += 1
        will_correct = rng.random() < correction_chance
        # Pick a mistake KIND — not just an adjacent substitution:
        #   substitution  - a neighbouring key instead of the intended one
        #   doubling      - the key is accidentally repeated
        #   insertion     - an extra neighbour key slips in before the intended one
        #   transposition - this char and the next are typed in the wrong order
        kinds = ["substitution", "doubling", "insertion"]
        weights = [0.55, 0.16, 0.13]
        if nxt and char.isalpha() and nxt.isalpha() and nxt != char:
            kinds.append("transposition")
            weights.append(0.16)
        kind = rng.choices(kinds, weights=weights, k=1)[0]

        start = i  # first intended index of the error region
        if kind == "substitution":
            _stroke(_neighboring_char(char))
            i += 1
            wrong_on_screen = 1
        elif kind == "doubling":
            _stroke(char)
            _stroke(char)
            i += 1
            wrong_on_screen = 2
        elif kind == "insertion":
            _stroke(_neighboring_char(char))
            _stroke(char)
            i += 1
            wrong_on_screen = 2
        else:  # transposition
            _stroke(nxt)
            _stroke(char)
            i += 2
            wrong_on_screen = 2

        # Sometimes the typist runs a char or two PAST the error before noticing it.
        if kind != "transposition" and will_correct and i < n and rng.random() < 0.35:
            overrun = 1 if (i + 1 >= n or rng.random() < 0.7) else 2
            for _ in range(overrun):
                if i >= n:
                    break
                _stroke(text[i])
                i += 1
                wrong_on_screen += 1

        if will_correct:
            _react()
            for _ in range(wrong_on_screen):
                _control("Backspace")
            for j in range(start, i):  # retype the intended span (incl. any overrun)
                _stroke(text[j])
            corrections += 1
        # else: leave the mistake in place (rare; an honest uncorrected error).
        typed += i - start

    result = {"typed_chars": typed, "typos": typos, "corrections": corrections}
    if recorder is not None:
        recorder.record("type_text", metadata={"selector": label, **result})
    return result


def _click_hold_ms(rng: random.Random) -> int:
    """Human mouse-button hold (press->release) dwell, in ms.

    A real click is not instantaneous: the button is held ~50-120 ms (longer when
    relaxed, shorter when rushing). Bare ``page.mouse.click()`` / ``locator.click()``
    use a 0 ms hold, i.e. ``mousedown`` and ``mouseup`` share a timestamp — a trivial
    tell for any detector that watches pointer-event dwell. Pass this as ``delay``.
    """
    return int(lognormal_ms(rng, mean_ms=78.0, cv=0.40, lo=30.0, hi=200.0))


def click_element(
    page: Any,
    selector: Any,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Click an element with optional pre-click hover."""
    pointer = _pointer_plan(plan)
    rng = _session_rng(page, plan)

    # Handle random link selection for red-team multi-page browsing simulations
    if selector == "random_link":
        update_status_ticker(page, "🖱️ CLICKING", "Choosing random link...")
        try:
            links = page.locator("a[href]").all()
            valid_links = []
            for link in links:
                try:
                    if link.is_visible():
                        box = link.bounding_box()
                        if box and box["width"] > 10 and box["height"] > 10:
                            href = str(link.get_attribute("href") or "").lower()
                            text = str(link.inner_text() or "").lower()
                            # Filter out utility/external/auth links to stay on target site
                            if not any(k in href or k in text for k in ("logout", "signout", "share", "facebook", "twitter", "linkedin", "privacy", "terms", "cookie", "login", "register")):
                                valid_links.append((link, box))
                except Exception:
                    pass
            
            # Relax filters if no links matched
            if not valid_links:
                for link in links:
                    try:
                        if link.is_visible():
                            box = link.bounding_box()
                            if box and box["width"] > 5 and box["height"] > 5:
                                valid_links.append((link, box))
                    except Exception:
                        pass

            # If still empty, scroll down a bit and try to find links again
            if not valid_links:
                page.evaluate("window.scrollBy(0, 400)")
                page.wait_for_timeout(500)
                links = page.locator("a[href]").all()
                for link in links:
                    try:
                        if link.is_visible():
                            box = link.bounding_box()
                            if box and box["width"] > 5 and box["height"] > 5:
                                valid_links.append((link, box))
                    except Exception:
                        pass

            # Drop any chosen link that is actually a hidden honeypot, checking the
            # SAME element we'd click. Re-roll among the remaining candidates.
            chosen = None
            candidates = list(valid_links)
            while candidates:
                cand_loc, cand_box = rng.choice(candidates)
                reason = _detect_honeypot_reason(cand_loc)
                if reason:
                    _register_honeypot(page, "random_link", reason, recorder)
                    candidates = [c for c in candidates if c[0] is not cand_loc]
                    continue
                chosen = (cand_loc, cand_box)
                break

            if chosen:
                locator, box = chosen
                locator = locator.first if hasattr(locator, "first") else locator
                cx = box["x"] + box["width"] * rng.uniform(0.28, 0.72)
                cy = box["y"] + box["height"] * rng.uniform(0.28, 0.72)
                update_status_ticker(page, "🖱️ CLICKING", f"Clicking: random link at ({round(cx)}, {round(cy)})")
                move_meta = move_pointer(page, cx, cy, plan, recorder=recorder, target_w=box.get("width"), target_h=box.get("height"))
                locator.click(delay=_click_hold_ms(rng))
                result = {
                    "hovered": False,
                    "move_style": (move_meta or {}).get("style") or pointer.get("move_style", "direct"),
                    "clicked_random_link": True,
                }
                if move_meta:
                    result["move"] = move_meta
                if recorder is not None:
                    recorder.record("click", metadata={"selector": "random_link", **result})
                return result
            else:
                # Fallback: Click a safe spot on the page instead of crashing
                cx = rng.uniform(300, 600)
                cy = rng.uniform(200, 500)
                update_status_ticker(page, "🖱️ CLICKING", f"Clicking fallback spot ({round(cx)}, {round(cy)})")
                move_meta = move_pointer(page, cx, cy, plan, recorder=recorder)
                page.mouse.click(cx, cy, delay=_click_hold_ms(rng))
                result = {
                    "hovered": False,
                    "move_style": (move_meta or {}).get("style") or pointer.get("move_style", "direct"),
                    "clicked_fallback_spot": True,
                }
                if move_meta:
                    result["move"] = move_meta
                if recorder is not None:
                    recorder.record("click", metadata={"selector": "fallback_spot", **result})
                return result
        except Exception:
            # Fallback to safe spot if anything fails
            cx = rng.uniform(300, 600)
            cy = rng.uniform(200, 500)
            move_pointer(page, cx, cy, plan, recorder=recorder)
            page.mouse.click(cx, cy, delay=_click_hold_ms(rng))
            return {"clicked_fallback_spot": True}

    # Resolve to the concrete element we will click. For string selectors that can
    # match multiple nodes (e.g. "a[href], button"), narrow to ``.first`` so the
    # click never throws Playwright strict-mode "resolved to N elements".
    if isinstance(selector, str):
        locator = page.locator(selector).first
        label = selector
    else:
        locator = selector.first if hasattr(selector, "first") else selector
        label = "element_handle"

    # --- Stateful Honeypot Evasion (Layer 4 Defense) ---
    # Run the trap test on the SAME element we just resolved (not a fresh
    # document.querySelector first match), so we evade the element we'd actually click.
    reason = _detect_honeypot_reason(locator)
    if reason:
        _register_honeypot(page, label, reason, recorder)
        return {"hovered": False, "honeypot_evaded": True, "honeypot_reason": reason}

    update_status_ticker(page, "🖱️ CLICKING", f"Clicking: {label}")

    hovered = False
    move_meta: dict[str, Any] | None = None
    click_xy: tuple[float, float] | None = None
    try:
        box = locator.bounding_box()
        if box:
            cx = box["x"] + box["width"] * rng.uniform(0.28, 0.72)
            cy = box["y"] + box["height"] * rng.uniform(0.28, 0.72)
            move_meta = move_pointer(page, cx, cy, plan, recorder=recorder, target_w=box.get("width"), target_h=box.get("height"))
            # Press at the cursor's REALIZED landing point (move_pointer applies
            # human endpoint imprecision). Letting Playwright's locator.click()
            # run would silently recenter to the element middle, erasing that
            # imprecision and producing a final teleport/snap right before click.
            ex = float((move_meta or {}).get("x", cx))
            ey = float((move_meta or {}).get("y", cy))
            click_xy = (ex, ey)
            # Sometimes settle on the target for a beat first (hover-before-click
            # intent) — a human pause, not an instant native hover jump.
            if rng.random() < float(pointer.get("hover_before_click_chance", 0.0)):
                hovered = True
                page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=320.0, cv=0.5, lo=140.0, hi=900.0)))
    except Exception:
        click_xy = None

    if click_xy is not None:
        page.mouse.click(click_xy[0], click_xy[1], delay=_click_hold_ms(rng))
    else:
        # No bounding box (e.g. zero-size/odd element): fall back to the locator
        # click so the action still lands.
        locator.click(delay=_click_hold_ms(rng))
    result = {
        "hovered": hovered,
        "move_style": (move_meta or {}).get("style") or pointer.get("move_style", "direct"),
    }
    if move_meta:
        result["move"] = move_meta
    if recorder is not None:
        recorder.record("click", metadata={"selector": str(selector), **result})
    return result


def hover_element(
    page: Any,
    selector: Any,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Hover an element and dwell for a bounded interval."""
    hover = plan.get("hover") if isinstance(plan, dict) else {}
    hover = hover if isinstance(hover, dict) else {}
    dwell_ms = _bounded_int(hover.get("hover_dwell_ms"), lower=150, upper=1_500, default=450)
    
    if isinstance(selector, str):
        locator = page.locator(selector).first
        label = selector
    else:
        locator = selector.first if hasattr(selector, "first") else selector
        label = "element_handle"

    # Same-element honeypot guard before hovering.
    reason = _detect_honeypot_reason(locator)
    if reason:
        _register_honeypot(page, label, reason, recorder)
        return {"hovered": False, "honeypot_evaded": True, "honeypot_reason": reason}

    update_status_ticker(page, "👁️ HOVERING", f"Hovering: {label} (dwell {dwell_ms}ms)")

    try:
        box = locator.bounding_box()
        if box:
            rng = _session_rng(page, plan)
            hx = box["x"] + box["width"] * rng.uniform(0.25, 0.75)
            hy = box["y"] + box["height"] * rng.uniform(0.25, 0.75)
            move_pointer(page, hx, hy, plan, recorder=recorder, target_w=box.get("width"), target_h=box.get("height"))
    except Exception:
        pass
    # move_pointer's final page.mouse.move already dispatches the hover/mouseover
    # over the element; a following locator.hover() would re-snap the cursor to the
    # element center, undoing the human approach. Just dwell here.
    page.wait_for_timeout(dwell_ms)
    result = {"selector": str(selector), "hover_dwell_ms": dwell_ms}
    if recorder is not None:
        recorder.record("hover", metadata=result)
    return result


def maybe_expand_comments(
    page: Any,
    selectors: tuple[str, ...],
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Best-effort bounded comment expansion using caller-provided selectors."""
    hover = plan.get("hover") if isinstance(plan, dict) else {}
    hover = hover if isinstance(hover, dict) else {}
    max_expansions = _bounded_int(hover.get("max_expansions"), lower=0, upper=3, default=1)
    chance = _bounded_float(hover.get("expand_comments_chance"), lower=0.0, upper=0.35, default=0.0)
    rng = _session_rng(page, plan)
    clicked = 0
    attempted = 0
    for selector in selectors:
        if clicked >= max_expansions:
            break
        attempted += 1
        if rng.random() > chance:
            continue
        try:
            # Route through the humanized click so comment-expand lands via a
            # real cursor approach, not an instant center-snap locator click.
            click_element(page, selector, plan, recorder=recorder)
        except Exception:
            continue
        clicked += 1
        if recorder is not None:
            recorder.record("expand_comments", metadata={"selector": selector})
    return {"attempted": attempted, "expanded": clicked, "max_expansions": max_expansions}


def maybe_backtrack(page: Any, plan: dict[str, Any] | None = None, *, recorder: SessionRecorder | None = None) -> dict[str, Any]:
    """Occasionally step back from a page when explicitly allowed by plan."""
    backtracking = plan.get("backtracking") if isinstance(plan, dict) else {}
    backtracking = backtracking if isinstance(backtracking, dict) else {}
    chance = _bounded_float(backtracking.get("micro_abandon_chance"), lower=0.0, upper=0.25, default=0.0)
    max_backtracks = _bounded_int(backtracking.get("max_backtracks"), lower=0, upper=2, default=0)
    should_backtrack = max_backtracks > 0 and _session_rng(page, plan).random() < chance
    if should_backtrack:
        page.go_back(wait_until="domcontentloaded")
    result = {"backtracked": should_backtrack, "max_backtracks": max_backtracks}
    if recorder is not None:
        recorder.record("backtrack" if should_backtrack else "backtrack_skipped", metadata=result)
    return result


def mobile_swipe(
    page: Any,
    plan: dict[str, Any] | None = None,
    pass_index: int = 0,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Perform a mobile-style swipe only when mobile gestures are enabled."""
    mobile = plan.get("mobile") if isinstance(plan, dict) else {}
    mobile = mobile if isinstance(mobile, dict) else {}
    if not mobile.get("enabled"):
        result = {"swiped": False, "reason": "mobile_disabled"}
        if recorder is not None:
            recorder.record("mobile_swipe_skipped", metadata=result)
        return result
    max_swipes = _bounded_int(mobile.get("max_swipes"), lower=0, upper=4, default=0)
    if pass_index >= max_swipes:
        result = {"swiped": False, "reason": "max_swipes_reached", "max_swipes": max_swipes}
        if recorder is not None:
            recorder.record("mobile_swipe_skipped", metadata=result)
        return result
    delta_y = planned_scroll_delta(plan, pass_index, 520)
    page.mouse.wheel(0, delta_y)
    result = {"swiped": True, "delta_y": delta_y, "gesture_style": mobile.get("gesture_style", "short_swipe")}
    if recorder is not None:
        recorder.record("mobile_swipe", metadata=result)
    return result


def _pointer_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(plan, dict):
        p = plan.get("pointer")
        if isinstance(p, dict):
            return p
    return {}


def _viewport_center(page: Any) -> tuple[float, float]:
    """Best-effort viewport center; used as the cursor's first-move origin."""
    try:
        vp = page.viewport_size
        if isinstance(vp, dict) and vp.get("width") and vp.get("height"):
            return float(vp["width"]) / 2.0, float(vp["height"]) / 2.0
    except Exception:
        logger.exception("[interaction_primitives] viewport_size read failed; using default center")
    return 640.0, 400.0


def _get_cursor(page: Any) -> tuple[float, float]:
    """Read the tracked cursor position, defaulting to viewport center on first move."""
    cur = getattr(page, "_imposter_cursor", None)
    if isinstance(cur, (tuple, list)) and len(cur) == 2:
        try:
            return float(cur[0]), float(cur[1])
        except (TypeError, ValueError):
            pass
    return _viewport_center(page)


def _set_cursor(page: Any, x: float, y: float) -> None:
    try:
        page._imposter_cursor = (float(x), float(y))
    except Exception:
        logger.exception("[interaction_primitives] could not persist _imposter_cursor on page")


def _bezier_point(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    """Cubic Bezier: B(t) = (1-t)^3 P0 + 3(1-t)^2 t P1 + 3(1-t) t^2 P2 + t^3 P3."""
    mt = 1.0 - t
    a = mt * mt * mt
    b = 3.0 * mt * mt * t
    c = 3.0 * mt * t * t
    d = t * t * t
    x = a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0]
    y = a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]
    return x, y


def _bezier_controls(
    p0: tuple[float, float],
    p3: tuple[float, float],
    rng: random.Random,
    *,
    wobble_cap: float,
    bow: float = 0.14,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Build P1/P2 offset PERPENDICULAR to the P0->P3 vector at ~1/3 and ~2/3 along.

    Hand reaches do not travel in straight lines; they bow gently to one side, the
    curvature being a person-stable trait. ``bow`` is the identity's baseline arc
    fraction (offset as a fraction of travel distance); the magnitude is then jittered
    per draw from the advancing session RNG so longer moves bow more and no two moves
    bow identically. P1/P2 usually bow the same way (a gentle C arc); occasionally
    opposite (an S-curve), matching observed cursor-path variety.
    """
    dx = p3[0] - p0[0]
    dy = p3[1] - p0[1]
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return p3, p3
    # Unit perpendicular to the travel direction.
    perp_x = -dy / dist
    perp_y = dx / dist

    b = max(0.0, float(bow))
    amp = dist * b * rng.uniform(0.7, 1.4)
    if wobble_cap > 0.0:
        amp = min(amp, wobble_cap)

    sign1 = 1.0 if rng.random() < 0.5 else -1.0
    sign2 = sign1 if rng.random() < 0.7 else -sign1
    off1 = amp * sign1 * rng.uniform(0.7, 1.0)
    off2 = amp * sign2 * rng.uniform(0.7, 1.0)

    p1 = (p0[0] + dx / 3.0 + perp_x * off1, p0[1] + dy / 3.0 + perp_y * off1)
    p2 = (p0[0] + 2.0 * dx / 3.0 + perp_x * off2, p0[1] + 2.0 * dy / 3.0 + perp_y * off2)
    return p1, p2


def _curvature(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    """Approximate the local path curvature κ = 1/R at point ``b`` from neighbours.

    Uses the circumscribed-circle formula κ = 4·Area(abc) / (|ab|·|bc|·|ca|). Used to
    apply the 2/3 power law (slow through tight curves).
    """
    abx, aby = b[0] - a[0], b[1] - a[1]
    cbx, cby = b[0] - c[0], b[1] - c[1]
    acx, acy = c[0] - a[0], c[1] - a[1]
    area2 = abs(abx * (-cby) - (-cbx) * aby)  # 2*area = |ab x cb|
    d_ab = math.hypot(abx, aby)
    d_cb = math.hypot(cbx, cby)
    d_ac = math.hypot(acx, acy)
    denom = d_ab * d_cb * d_ac
    if denom < 1e-9:
        return 0.0
    return (2.0 * area2) / denom


def _velocity_progress(u: float, skew: float) -> float:
    """Time→displacement reparameterization combining minimum-jerk with mild skew.

    Base is the minimum-jerk profile (Flash & Hogan, 1985), a symmetric bell-shaped
    velocity. Real reaches are slightly asymmetric with a longer deceleration phase
    (the corrective tail); ``skew`` in [0, ~0.25] blends in an ease-out term
    ``1-(1-u)^3`` that reaches mid-displacement earlier, lengthening the decel phase.
    Monotonic with progress(0)=0, progress(1)=1.
    """
    base = min_jerk_progress(u)
    if skew <= 0.0:
        return base
    fast = 1.0 - (1.0 - u) ** 3
    return (1.0 - skew) * base + skew * fast


def _emit_bezier(
    page: Any,
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    *,
    steps: int,
    rng: random.Random,
    total_ms: float,
    clock: list[float],
    tremor_hz: float,
    tremor_amp_px: float,
    power_law_gain: float,
    min_jerk_skew: float,
) -> tuple[float, float]:
    """Sample and emit a Bezier segment honoring three human motor invariants.

    1. VELOCITY PROFILE — samples are placed in time via :func:`_velocity_progress`
       (minimum-jerk + skew), so the cursor accelerates, peaks, then decelerates.
    2. 2/3 POWER LAW — the per-sample dwell is scaled by local curvature
       (:func:`two_thirds_power_dwell_scale`): the cursor slows through tight curves
       and speeds up on straight segments (Lacquaniti et al., 1983). Dwells are
       normalized so the segment takes ~``total_ms``.
    3. MOTOR NOISE — two complementary, physiologically-grounded components, both
       scaled by ``tremor_amp_px`` (so ``amp == 0`` is a perfectly clean baseline):
         * SIGNAL-DEPENDENT noise (Harris & Wolpert, 1998): a small Gaussian
           positional wander whose std tracks the LOCAL speed, so the path is
           noisiest mid-flight and quiets onto the target. It is AR(1)-smoothed
           (a wavering hand, not white fuzz) and vanishes at the endpoint.
         * SETTLE TREMOR (Elble & Koller, 1990): an ~8–12 Hz oscillation during
           the low-velocity homing phase. It is ELLIPTICAL (independent
           perpendicular + longitudinal components, not a single line) and a
           NARROWBAND STOCHASTIC process — component frequencies/amplitudes are
           drawn per move and a slow phase random-walk lets the instantaneous
           frequency wander, so the cross-move spectrum is a band, not two fixed
           lines. ``clock`` carries elapsed ms across segments so phase stays
           continuous.
    4. INTEGER, ERROR-DIFFUSED EMISSION — real pointers report INTEGER client
       coordinates, so each sample is rounded to whole pixels HERE rather than
       depending on the transport to quantize floats (uncontrolled, and a
       fractional ``clientX`` is itself an oddity for a mouse). The fractional
       remainder is carried forward (1-D error diffusion), so a sub-pixel settle
       tremor survives as correctly-timed +/-1px steps instead of being truncated
       to nothing or degenerating into a regular stair-step.

    The final sample lands on (rounded) P3 sans noise, so the endpoint is exact.
    """
    n = max(2, int(steps))
    # Pass 1: positions along the curve under the velocity reparam.
    raw: list[tuple[float, float]] = []
    for i in range(1, n + 1):
        u = i / n
        t = _velocity_progress(u, min_jerk_skew)
        raw.append(_bezier_point(p0, p1, p2, p3, t))

    # Curvature per interior sample (endpoints reuse neighbours).
    curv: list[float] = []
    for i in range(len(raw)):
        a = raw[i - 1] if i - 1 >= 0 else p0
        b = raw[i]
        c = raw[i + 1] if i + 1 < len(raw) else raw[i]
        curv.append(_curvature(a, b, c))
    positive = [k for k in curv if k > 1e-9]
    ref = (sum(positive) / len(positive)) if positive else 1e-6

    # Per-step dwell weights from the 2/3 power law, normalized to total_ms.
    weights = [two_thirds_power_dwell_scale(k, ref_curvature=ref, gain=power_law_gain) for k in curv]
    wsum = sum(weights) or float(len(weights))
    dwell_ms = [max(1.0, total_ms * (w / wsum)) for w in weights]

    amp = max(0.0, float(tremor_amp_px))
    hz = max(1.0, float(tremor_hz))
    n_raw = len(raw)

    # --- Per-MOVE settle-tremor parameters -----------------------------------------
    # Real physiological tremor is a NARROWBAND STOCHASTIC process, not a fixed pair
    # of sinusoids. Two prior tells are removed here:
    #   * the spectrum was two fixed lines at a fixed 1.27 ratio (a dead giveaway on
    #     an FFT pooled across moves) -> draw both component frequencies AND their
    #     amplitude split PER MOVE, and let a slow phase random-walk make the
    #     instantaneous frequency WANDER within the move (each line becomes a band);
    #   * the wobble was rank-1 (a single perpendicular line) -> add an independent
    #     LONGITUDINAL component so it traces a small ELLIPSE, like a real 2-D hand
    #     tremor, instead of "exploding" along one axis.
    f_perp1 = hz * rng.uniform(0.85, 1.15)
    f_perp2 = f_perp1 * rng.uniform(1.18, 1.45)
    f_long = hz * rng.uniform(0.80, 1.18)
    split = rng.uniform(0.58, 0.78)          # energy fraction in the primary line
    long_ratio = rng.uniform(0.35, 0.55)     # longitudinal amp vs perpendicular
    ph_p1 = rng.uniform(0.0, 2.0 * math.pi)
    ph_p2 = rng.uniform(0.0, 2.0 * math.pi)
    ph_l = rng.uniform(0.0, 2.0 * math.pi)
    drift_step = 0.045                        # rad/sample slow frequency wander
    drift_p = 0.0
    drift_l = 0.0
    # Randomized settle onset PER MOVE — no constant 0.78 boundary across every move,
    # which is itself a tell. The damped Hann burst still returns to 0 at the endpoint.
    onset = rng.uniform(0.55, 0.74)

    # --- Signal-dependent (Harris & Wolpert, 1998) motor noise ---------------------
    # Endpoint variance grows with commanded speed, so the path is NOISIEST mid-flight
    # and quiets as the hand decelerates onto the target (the INVERSE of the old
    # settle-only model). Std tracks the local speed; an AR(1) filter makes it a
    # wavering hand rather than white per-sample fuzz. It vanishes at the endpoint
    # (speed -> 0) and on a zero-length move, keeping the arrival clean. Mid-flight it
    # is supra-pixel for typical amplitudes; the fine settle tremor is preserved
    # across the whole-pixel emission by error diffusion (see invariant 4), not left
    # to the transport's uncontrolled float rounding.
    step_dist = [
        math.hypot(raw[i + 1][0] - raw[i][0], raw[i + 1][1] - raw[i][1])
        for i in range(n_raw - 1)
    ]
    max_step = max(step_dist) if step_dist else 0.0
    noise_amp = amp * 1.4                     # ~peak (mid-flight) noise std, in px
    ar = 0.7                                  # temporal correlation of motor noise
    ar_b = math.sqrt(max(0.0, 1.0 - ar * ar))
    ou_x = 0.0
    ou_y = 0.0

    chord_dx, chord_dy = (p3[0] - p0[0]), (p3[1] - p0[1])
    chord_len = math.hypot(chord_dx, chord_dy)
    if chord_len > 1e-6:
        chord_tan = (chord_dx / chord_len, chord_dy / chord_len)
        chord_perp = (-chord_dy / chord_len, chord_dx / chord_len)
    else:
        # Zero-length move: no travel direction and (below) zero speed => no motion.
        chord_tan = (0.0, 0.0)
        chord_perp = (0.0, 0.0)

    last = p3
    # Integer error-diffusion accumulators (invariant 4): carry the sub-pixel
    # remainder forward so whole-pixel emission preserves the fine tremor instead of
    # truncating it away. Reset per move, so each reach starts from a clean slate.
    res_x = 0.0
    res_y = 0.0
    for i in range(n_raw):
        px, py = raw[i]
        if amp > 0.0 and i < n_raw - 1:
            # Stable travel axes: prefer the local tangent, fall back to the segment
            # chord when consecutive samples are ~coincident (settle), so the wobble
            # stays a coherent 2-D oscillation instead of spraying in random dirs.
            tx, ty = (raw[i + 1][0] - px, raw[i + 1][1] - py)
            tlen = math.hypot(tx, ty)
            if tlen >= 0.5:
                tan_x, tan_y = tx / tlen, ty / tlen
                perp_x, perp_y = -tan_y, tan_x
            else:
                tan_x, tan_y = chord_tan
                perp_x, perp_y = chord_perp

            # (1) Signal-dependent noise: speed-scaled, AR(1)-smoothed, whole path.
            norm_speed = (step_dist[i] / max_step) if max_step > 1e-9 else 0.0
            sd = noise_amp * norm_speed
            ou_x = ar * ou_x + ar_b * sd * rng.gauss(0.0, 1.0)
            ou_y = ar * ou_y + ar_b * sd * rng.gauss(0.0, 1.0)

            # (2) Settle tremor: elliptical, wandering narrowband, damped to 0 at end.
            frac = i / (n_raw - 1)
            if frac < onset:
                eff = 0.0
            else:
                w = (frac - onset) / (1.0 - onset)
                eff = amp * math.sin(math.pi * w)  # 0 -> peak -> 0 (clean endpoint)
            jx = jy = 0.0
            if eff > 0.0:
                t_s = clock[0] / 1000.0
                drift_p += rng.gauss(0.0, drift_step)
                drift_l += rng.gauss(0.0, drift_step)
                osc_perp = (
                    split * math.sin(2.0 * math.pi * f_perp1 * t_s + ph_p1 + drift_p)
                    + (1.0 - split) * math.sin(2.0 * math.pi * f_perp2 * t_s + ph_p2 + drift_p)
                )
                osc_long = math.sin(2.0 * math.pi * f_long * t_s + ph_l + drift_l)
                jx = perp_x * eff * osc_perp + tan_x * eff * long_ratio * osc_long
                jy = perp_y * eff * osc_perp + tan_y * eff * long_ratio * osc_long

            gx = px + ou_x + jx + res_x
            gy = py + ou_y + jy + res_y
            ix, iy = round(gx), round(gy)
            res_x, res_y = gx - ix, gy - iy
            page.mouse.move(ix, iy)
        else:
            # Clean sample (the exact endpoint, or an amp==0 baseline move): land on
            # the whole-pixel target and clear the residual so arrival is pixel-exact.
            res_x = res_y = 0.0
            page.mouse.move(round(px), round(py))
        last = (px, py)
        clock[0] += dwell_ms[i]
        if i < n_raw - 1:
            page.wait_for_timeout(int(round(dwell_ms[i])))
    return last


def move_pointer(
    page: Any,
    x: float,
    y: float,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
    target_w: float | None = None,
    target_h: float | None = None,
) -> dict[str, Any]:
    """Move the cursor to ``(x, y)`` along a human-realistic aimed movement.

    The trajectory models the published invariants of human aimed movement, drawing
    ALL randomness from the single advancing per-session RNG so that the same
    start/end never produces the same path (motor variability; Harris & Wolpert,
    1998):

    - DURATION follows Fitts's law: movement time grows with log2(distance/width)
      (Fitts, 1954). ``target_w`` (e.g. a button's width) sharpens the prediction;
      absent it, a nominal acquisition width is used.
    - The PRIMARY ballistic phase is a cubic Bezier (gentle person-stable arc) sampled
      with a minimum-jerk velocity profile and curvature-dependent (2/3 power-law)
      timing, with ~8-12 Hz physiological tremor superimposed (see ``_emit_bezier``).
    - SECONDARY corrective submovement(s) near the target follow the optimized-
      submovement model (Meyer et al., 1988): a short homing hop whose probability
      rises with the index of difficulty, plus the existing overshoot-and-return.
    - Intra-session FATIGUE slows the movement and grows tremor/endpoint scatter
      over a long session.

    The cursor ends exactly on the (imprecision-jittered) target, recorded on the page.
    """
    pointer = _pointer_plan(plan)
    rng = _session_rng(page, plan)
    drift = _fatigue(page, plan)

    # Physics knobs live on the plan's human_config (identity-derived, see
    # behavior_policy.build_identity_kinematics).
    hc = _human_config(plan)
    imprec = _bounded_float(
        hc.get("mouse_imprecision_px", pointer.get("imprecision_px")), lower=0.0, upper=20.0, default=3.0
    ) * drift["sloppiness"]
    max_steps = _bounded_int(hc.get("mouse_max_steps"), lower=12, upper=60, default=32)
    step_dt = _bounded_float(hc.get("mouse_step_delay_ms"), lower=1.0, upper=50.0, default=8.0)
    wobble_cap = _bounded_float(hc.get("mouse_wobble_max"), lower=0.0, upper=400.0, default=0.0)
    bow = _bounded_float(hc.get("mouse_curve_bow"), lower=0.0, upper=0.5, default=0.14)
    tremor_hz = _bounded_float(hc.get("tremor_hz"), lower=5.0, upper=14.0, default=10.0)
    tremor_amp = _bounded_float(hc.get("tremor_amp_px"), lower=0.0, upper=3.0, default=0.6) * drift["sloppiness"]
    pl_gain = _bounded_float(hc.get("power_law_gain"), lower=0.0, upper=1.0, default=0.8)
    mj_skew = _bounded_float(hc.get("min_jerk_skew"), lower=0.0, upper=0.45, default=0.12)
    fitts_a = _bounded_float(hc.get("fitts_a_ms"), lower=40.0, upper=400.0, default=100.0)
    fitts_b = _bounded_float(hc.get("fitts_b_ms"), lower=80.0, upper=400.0, default=140.0)
    ovr_ch = _bounded_float(
        hc.get("mouse_overshoot_chance", pointer.get("overshoot_chance")), lower=0.0, upper=0.5, default=0.08
    )
    corr_ch = _bounded_float(hc.get("corrective_submovement_chance"), lower=0.0, upper=0.95, default=0.45)
    corr_max = _bounded_int(hc.get("corrective_submovement_max"), lower=0, upper=3, default=1)
    ovr_px = hc.get("mouse_overshoot_px")
    if isinstance(ovr_px, (list, tuple)) and len(ovr_px) == 2:
        ovr_lo, ovr_hi = float(ovr_px[0]), float(ovr_px[1])
    else:
        ovr_lo, ovr_hi = 4.0, 14.0

    # Jittered target (human endpoint imprecision) — the curve's true endpoint.
    tx = float(x) + rng.uniform(-imprec, imprec)
    ty = float(y) + rng.uniform(-imprec, imprec)
    target = (tx, ty)

    start = _get_cursor(page)
    dx = tx - start[0]
    dy = ty - start[1]
    dist = math.hypot(dx, dy)

    # Effective target width for Fitts: prefer the real element extent (the smaller
    # of width/height is the limiting acquisition dimension), else a nominal value.
    if target_w is not None or target_h is not None:
        w = min([d for d in (target_w, target_h) if d is not None] or [24.0])
        eff_w = max(6.0, float(w))
    else:
        eff_w = 24.0

    # Index of difficulty drives both movement time (Fitts) and how likely a homing
    # correction is needed (harder acquisitions => more corrective submovements).
    idx_diff = math.log2(dist / eff_w + 1.0) if dist > 0 else 0.0
    mt_ms = fitts_movement_time_ms(dist, eff_w, a_ms=fitts_a, b_ms=fitts_b) * drift["slowdown"]
    # Per-move speed variability. The Fitts time above is identity-stable, so
    # similar-distance moves would otherwise take near-identical time — a "too
    # consistent / slightly slow" feel and a low speed-variance signal. Draw a
    # multiplicative factor from the session RNG each move (log-normal, symmetric
    # in log space, clamped) so some moves are snappy and some are unhurried,
    # which is how a real hand behaves and what kinetics detectors expect.
    mt_ms *= max(0.6, min(1.7, math.exp(rng.gauss(0.0, 0.22))))
    # Step count is the movement time divided by the per-sample cadence, bounded.
    steps = max(8, min(max_steps, int(round(mt_ms / max(1.0, step_dt)))))

    clock = [0.0]  # shared elapsed-ms clock so tremor phase is continuous across hops.
    overshot = rng.random() < ovr_ch

    def _emit(p_from: tuple[float, float], p_to: tuple[float, float], n_steps: int, segment_ms: float) -> None:
        p1, p2 = _bezier_controls(p_from, p_to, rng, wobble_cap=wobble_cap, bow=bow)
        _emit_bezier(
            page,
            p_from,
            p1,
            p2,
            p_to,
            steps=n_steps,
            rng=rng,
            total_ms=segment_ms,
            clock=clock,
            tremor_hz=tremor_hz,
            tremor_amp_px=tremor_amp,
            power_law_gain=pl_gain,
            min_jerk_skew=mj_skew,
        )

    submovements = 1
    if overshot and dist > 1e-6:
        # Ballistic phase deliberately overshoots, then a corrective return.
        ext = rng.uniform(ovr_lo, ovr_hi)
        ux, uy = dx / dist, dy / dist
        over_pt = (tx + ux * ext, ty + uy * ext)
        _emit(start, over_pt, steps, mt_ms)
        page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=max(step_dt * 3.0, 20.0), cv=0.4, lo=10.0, hi=160.0)))
        corr_steps = max(6, steps // 3)
        _emit(over_pt, target, corr_steps, mt_ms * 0.35)
        submovements = 2
        used = "ballistic_overshoot_correct"
    else:
        # Primary ballistic phase aims slightly short of the target (undershoot bias
        # is the common case in Meyer's model), then optional homing submovements.
        if dist > 1e-6 and corr_ch > 0.0:
            undershoot = rng.uniform(0.03, 0.10)
            primary_pt = (start[0] + dx * (1.0 - undershoot), start[1] + dy * (1.0 - undershoot))
        else:
            primary_pt = target
        _emit(start, primary_pt, steps, mt_ms)
        used = "ballistic"

        # Corrective submovements: probability rises with the index of difficulty.
        cur = primary_pt
        eff_corr_ch = min(0.95, corr_ch * (0.5 + 0.18 * idx_diff))
        for _ in range(corr_max):
            if cur == target:
                break
            if rng.random() >= eff_corr_ch:
                break
            # Perceive-decide-act gap before a homing correction. Floor at a
            # human reaction latency (~120ms); 8ms was physiologically impossible
            # and a cheap tell for kinetics detectors.
            page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=max(step_dt * 2.0, 120.0), cv=0.45, lo=120.0, hi=260.0)))
            corr_steps = max(5, steps // 4)
            _emit(cur, target, corr_steps, mt_ms * 0.30)
            cur = target
            submovements += 1
            used = "ballistic_correct"
            eff_corr_ch *= 0.4  # each successive correction is much less likely.

    _set_cursor(page, tx, ty)

    res = {
        "x": round(tx),
        "y": round(ty),
        "style": used,
        "imprecision_px": round(imprec, 2),
        "overshot": overshot,
        "submovements": submovements,
        "movement_time_ms": round(mt_ms, 1),
        "index_of_difficulty": round(idx_diff, 2),
        "steps": steps,
    }
    if recorder is not None:
        recorder.record("mouse_move", metadata=res)
    return res


# --- Visible synthetic cursor for "over the shoulder" / movie review only ---
# These are deliberately QA-only affordances. The red overlay makes the real
# trajectories (from move_pointer, raw moves in harness/scraper, goal runner etc.)
# obvious to a human watching a headed window or a recorded movie. The cursor
# is injected as a DOM element + style (no native pointer spoof that would be
# fingerprintable). In normal (visible=False) runs, these are never called and
# zero DOM/JS/globals are added.
#
# Used by: scripts/observe_human_mechanics.py (the dedicated movie harness),
# the visible=True path in loaders/linkedin_feed_scraper.py (for Watch + linkedin),
# and GenericWebAdapter + LinkedInAdapter in checker.py for the "Watch" button.
#
# The movie recording (record_video_dir on context) captures the overlay + the
# page, giving the "save as a movie and view the mouse later" behavior.


def inject_synthetic_cursor(page: Any) -> None:
    """Create (idempotent) the large bright red QA cursor overlay in the page.

    Uses createElement (robust to CSP/navigations) + add_init_script for future
    pages + direct evaluate for the current document. Features high-precision 
    canvas drawing for color-coded velocity/acceleration trails and real-time
    telemetry overlay.
    """
    js = """
    (function(){
        // Only the top document renders the overlay. add_init_script runs in
        // EVERY frame (LinkedIn embeds ad/util iframes), so without this guard
        // each iframe spawns its own cursor + telemetry + ticker, which shows
        // up as duplicated "HUMAN MOUSE" markers / panels.
        if (window.top !== window.self) { return; }
        // Store state persistently on window so it survives re-injection.
        // Seed the start position from sessionStorage so the cursor RESUMES where
        // it was before a navigation instead of snapping back to a fixed point on
        // the left edge on every page load (a human's pointer does not teleport
        // when a page changes). sessionStorage survives same-origin navigations.
        if (!window.__human_cursor_state) {
            // Spawn from a RANDOM edge (not always the same top-left spot, which
            // is a cheap tell). The cursor enters just off one of the four edges
            // and the first ballistic move brings it on-screen from there.
            const __vw = window.innerWidth || 1280, __vh = window.innerHeight || 800;
            let __sx, __sy;
            const __edge = Math.floor(Math.random() * 4);
            if (__edge === 0)      { __sx = Math.random() * __vw; __sy = -12; }            // top
            else if (__edge === 1) { __sx = __vw + 12; __sy = Math.random() * __vh; }      // right
            else if (__edge === 2) { __sx = Math.random() * __vw; __sy = __vh + 12; }      // bottom
            else                   { __sx = -12; __sy = Math.random() * __vh; }            // left
            __sx = Math.round(__sx); __sy = Math.round(__sy);
            try {
                const __saved = sessionStorage.getItem('__human_cursor_pos__');
                if (__saved) {
                    const __p = JSON.parse(__saved);
                    if (__p && typeof __p.x === 'number' && typeof __p.y === 'number') {
                        __sx = __p.x; __sy = __p.y;
                    }
                }
            } catch (e) {}
            window.__human_cursor_state = {
                points: [],
                lastX: __sx,
                lastY: __sy,
                lastTime: performance.now(),
                lastVel: 0,
                stepCount: 0,
                currentAction: 'INITIALIZING...',
                currentDetails: 'Waiting for simulation start...',
                timelineEvents: []
            };
        }
        
        const state = window.__human_cursor_state;
        
        function injectElements() {
            if (!document.documentElement) {
                setTimeout(injectElements, 50);
                return;
            }
            
            let style = document.getElementById('__human_cursor_style__');
            if (!style) {
                style = document.createElement('style');
                style.id = '__human_cursor_style__';
                style.textContent = `
                    #__human_cursor__ {
                        position: fixed !important;
                        left: 0; top: 0;
                        width: 22px; height: 22px;
                        pointer-events: none !important;
                        z-index: 2147483647 !important;
                        /* Align the SVG arrow tip (4,2) to the real point. */
                        transform: translate(-4px, -2px);
                        filter: drop-shadow(0 1px 2px rgba(0,0,0,0.5));
                    }
                    #__human_cursor__ svg { display: block; }
                    #__human_cursor__ .label {
                        position: absolute;
                        left: 52px; top: 4px;
                        background: #ff1a1a;
                        color: #fff;
                        font: 700 11px/1 system-ui, sans-serif;
                        padding: 2px 6px;
                        border-radius: 3px;
                        white-space: nowrap;
                        box-shadow: 0 1px 2px rgba(0,0,0,0.4);
                    }
                    /* Speedometer/Telemetry overlay */
                    #__human_cursor_telemetry__ {
                        position: fixed !important;
                        bottom: 20px;
                        right: 20px;
                        background: rgba(15, 23, 42, 0.85) !important;
                        border: 1px solid rgba(244, 63, 94, 0.4) !important;
                        border-radius: 8px !important;
                        padding: 10px 14px !important;
                        color: #38bdf8 !important;
                        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
                        font-size: 11px !important;
                        line-height: 1.4 !important;
                        z-index: 2147483640 !important;
                        pointer-events: none !important;
                        box-shadow: 0 4px 12px rgba(0,0,0,0.5) !important;
                        backdrop-filter: blur(4px) !important;
                        width: 220px !important;
                    }
                    #__human_cursor_telemetry__ .title {
                        color: #f43f5e !important;
                        font-weight: bold !important;
                        text-transform: uppercase !important;
                        letter-spacing: 0.05em !important;
                        border-bottom: 1px solid rgba(244, 63, 94, 0.2) !important;
                        padding-bottom: 4px !important;
                        margin-bottom: 6px !important;
                    }
                    #__human_cursor_telemetry__ .row {
                        display: flex !important;
                        justify-content: space-between !important;
                        margin-bottom: 2px !important;
                    }
                    #__human_cursor_telemetry__ .val {
                        color: #10b981 !important;
                        font-weight: bold !important;
                    }
                    
                    /* Futuristic status ticker / timeline */
                    #__imposter5_ticker__ {
                        position: fixed !important;
                        bottom: 20px !important;
                        left: 20px !important;
                        width: calc(100vw - 280px) !important;
                        background: rgba(15, 23, 42, 0.9) !important;
                        border: 1px solid rgba(244, 63, 94, 0.4) !important;
                        border-radius: 8px !important;
                        padding: 12px 16px !important;
                        color: #f1f5f9 !important;
                        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
                        font-size: 12px !important;
                        z-index: 2147483641 !important;
                        pointer-events: none !important;
                        box-shadow: 0 4px 12px rgba(0,0,0,0.5) !important;
                        backdrop-filter: blur(6px) !important;
                        display: flex !important;
                        flex-direction: column !important;
                        gap: 8px !important;
                    }
                    #__imposter5_ticker__ .header {
                        display: flex !important;
                        align-items: center !important;
                        justify-content: space-between !important;
                        border-bottom: 1px solid rgba(244, 63, 94, 0.2) !important;
                        padding-bottom: 6px !important;
                    }
                    #__imposter5_ticker__ .title-container {
                        display: flex !important;
                        align-items: center !important;
                        gap: 8px !important;
                    }
                    #__imposter5_ticker__ .pulse {
                        width: 8px; height: 8px;
                        background: #f43f5e;
                        border-radius: 50%;
                        box-shadow: 0 0 8px #f43f5e;
                        animation: __pulse__ 1.5s infinite;
                    }
                    #__imposter5_ticker__ .brand {
                        color: #f43f5e !important;
                        font-weight: bold !important;
                        text-transform: uppercase !important;
                        letter-spacing: 0.05em !important;
                    }
                    #__imposter5_ticker__ .action {
                        color: #38bdf8 !important;
                        font-weight: bold !important;
                        text-transform: uppercase !important;
                    }
                    #__imposter5_ticker__ .details {
                        color: #94a3b8 !important;
                        font-size: 11px !important;
                    }
                    #__imposter5_ticker__ .timeline-title {
                        color: #64748b !important;
                        font-size: 10px !important;
                        text-transform: uppercase !important;
                        font-weight: bold !important;
                        margin-top: 4px !important;
                        margin-bottom: 2px !important;
                    }
                    #__imposter5_ticker__ .timeline {
                        display: flex !important;
                        flex-direction: column !important;
                        gap: 2px !important;
                        max-height: 80px !important;
                        overflow: hidden !important;
                    }
                    
                    @keyframes __pulse__ {
                        0% { opacity: 0.4; box-shadow: 0 0 2px #f43f5e; }
                        50% { opacity: 1; box-shadow: 0 0 10px #f43f5e; }
                        100% { opacity: 0.4; box-shadow: 0 0 2px #f43f5e; }
                    }
                `;
                document.documentElement.appendChild(style);
            }
            
            let canvas = document.getElementById('__human_cursor_canvas__');
            // Info overlays (velocity/accel trail, telemetry panel, status ticker)
            // are intentionally NOT baked into the movie. Their data lives in the
            // SessionRecorder event log for a post-hoc playback tool. Only the red
            // mouse cursor is rendered (the one thing we verify for accuracy).
            // Escape hatch: set window.__imposter5_info_overlays = true before
            // injection to bring them back.
            if (!canvas && window.__imposter5_info_overlays) {
                canvas = document.createElement('canvas');
                canvas.id = '__human_cursor_canvas__';
                canvas.style.cssText = `
                    position: fixed !important;
                    left: 0; top: 0;
                    width: 100vw; height: 100vh;
                    pointer-events: none !important;
                    z-index: 2147483645 !important;
                `;
                document.documentElement.appendChild(canvas);
                
                const ctx = canvas.getContext('2d');
                function resizeCanvas() {
                    canvas.width = window.innerWidth * window.devicePixelRatio;
                    canvas.height = window.innerHeight * window.devicePixelRatio;
                    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
                }
                window.addEventListener('resize', resizeCanvas);
                resizeCanvas();
                
                function drawTrail() {
                    if (!document.getElementById('__human_cursor_canvas__')) return;
                    const now = performance.now();
                    
                    // Periodically ensure canvas size is non-zero and matches window size
                    const expectedW = window.innerWidth * window.devicePixelRatio;
                    const expectedH = window.innerHeight * window.devicePixelRatio;
                    if (canvas.width !== expectedW || canvas.height !== expectedH) {
                        resizeCanvas();
                    }
                    
                    while (state.points.length > 0 && now - state.points[0].timestamp > 3000) {
                        state.points.shift();
                    }
                    
                    ctx.clearRect(0, 0, canvas.width, canvas.height);
                    
                    if (state.points.length < 2) {
                        requestAnimationFrame(drawTrail);
                        return;
                    }
                    
                    for (let i = 1; i < state.points.length; i++) {
                        const p1 = state.points[i - 1];
                        const p2 = state.points[i];
                        
                        const age = now - p2.timestamp;
                        const alpha = Math.max(0, 1 - age / 3000);
                        
                        let color = 'rgba(6, 182, 212, ' + alpha + ')';
                        if (p2.vel > 1.5) {
                            color = 'rgba(244, 63, 94, ' + alpha + ')';
                        } else if (p2.vel > 0.5) {
                            color = 'rgba(139, 92, 246, ' + alpha + ')';
                        } else if (p2.vel > 0.1) {
                            color = 'rgba(16, 185, 129, ' + alpha + ')';
                        }
                        
                        ctx.beginPath();
                        ctx.moveTo(p1.x, p1.y);
                        ctx.lineTo(p2.x, p2.y);
                        ctx.strokeStyle = color;
                        ctx.lineWidth = Math.max(1, 4 * alpha);
                        ctx.lineCap = 'round';
                        ctx.stroke();
                        
                        if (i % 2 === 0) {
                            ctx.beginPath();
                            ctx.arc(p2.x, p2.y, Math.max(1.5, 3.5 * alpha), 0, Math.PI * 2);
                            ctx.fillStyle = color;
                            ctx.fill();
                        }
                    }
                    requestAnimationFrame(drawTrail);
                }
                requestAnimationFrame(drawTrail);
            }
            
            let cursor = document.getElementById('__human_cursor__');
            if (!cursor) {
                cursor = document.createElement('div');
                cursor.id = '__human_cursor__';
                cursor.setAttribute('aria-hidden', 'true');
                cursor.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 2 L4 18 L8.5 14 L11.4 20.4 L13.8 19.3 L10.9 13 L16.6 13 Z" fill="#ff2d2d" stroke="#ffffff" stroke-width="1.3" stroke-linejoin="round"/></svg>';
                document.documentElement.appendChild(cursor);
                cursor.style.left = (state.lastX | 0) + 'px';
                cursor.style.top = (state.lastY | 0) + 'px';
            }
            
            let tel = document.getElementById('__human_cursor_telemetry__');
            if (!tel && window.__imposter5_info_overlays) {
                tel = document.createElement('div');
                tel.id = '__human_cursor_telemetry__';
                tel.setAttribute('aria-hidden', 'true');
                tel.innerHTML = `
                    <div class="title">Telemetry Monitor</div>
                    <div class="row"><span>Position:</span><span id="__tel_pos__" class="val">${Math.round(state.lastX)}, ${Math.round(state.lastY)}</span></div>
                    <div class="row"><span>Velocity:</span><span id="__tel_vel__" class="val">${state.lastVel.toFixed(3)} px/ms</span></div>
                    <div class="row"><span>Acceleration:</span><span id="__tel_acc__" class="val">0 px/ms²</span></div>
                    <div class="row"><span>Micro-steps:</span><span id="__tel_steps__" class="val">${state.stepCount}</span></div>
                `;
                document.documentElement.appendChild(tel);
            }
            
            let ticker = document.getElementById('__imposter5_ticker__');
            if (!ticker && window.__imposter5_info_overlays) {
                ticker = document.createElement('div');
                ticker.id = '__imposter5_ticker__';
                ticker.setAttribute('aria-hidden', 'true');
                ticker.innerHTML = `
                    <div class="header">
                        <div class="title-container">
                            <div class="pulse"></div>
                            <span class="brand">IMPOSTER5 ACTIVE PROTOCOL</span>
                        </div>
                        <div id="__ticker_current_action__" class="action">${state.currentAction}</div>
                    </div>
                    <div id="__ticker_details__" class="details">${state.currentDetails}</div>
                    <div>
                        <div class="timeline-title">Event Timeline</div>
                        <div id="__ticker_timeline__" class="timeline"></div>
                    </div>
                `;
                document.documentElement.appendChild(ticker);
                
                const tlEl = document.getElementById('__ticker_timeline__');
                if (tlEl) {
                    state.timelineEvents.forEach(ev => {
                        const item = document.createElement('div');
                        item.style.cssText = 'color: #cbd5e1 !important; font-size: 10px !important; display: flex !important; gap: 6px !important;';
                        item.innerHTML = `<span style="color: #64748b !important;">[${ev.time}]</span> <span style="color: #f43f5e !important; font-weight: bold !important;">${ev.action}</span> <span>${ev.details}</span>`;
                        tlEl.appendChild(item);
                    });
                }
            }
        }
        
        window.__human_cursor_move = function(x, y) {
            const el = document.getElementById('__human_cursor__');
            if (el) {
                el.style.left = (x | 0) + 'px';
                el.style.top = (y | 0) + 'px';
            }
            
            const now = performance.now();
            const dx = x - state.lastX;
            const dy = y - state.lastY;
            const dist = Math.sqrt(dx*dx + dy*dy);
            const dt = now - state.lastTime;
            
            let vel = 0;
            let acc = 0;
            if (dt > 0) {
                vel = dist / dt;
                acc = (vel - state.lastVel) / dt;
            }
            
            state.stepCount++;
            
            // Trail is not rendered into the movie anymore; keep only a tiny
            // bounded buffer so long scheduled sessions never grow it unbounded.
            state.points.push({ x: x, y: y, timestamp: now, vel: vel, acc: acc });
            if (state.points.length > 200) { state.points.shift(); }
            
            const tPos = document.getElementById('__tel_pos__');
            const tVel = document.getElementById('__tel_vel__');
            const tAcc = document.getElementById('__tel_acc__');
            const tSteps = document.getElementById('__tel_steps__');
            if (tPos) tPos.textContent = `${Math.round(x)}, ${Math.round(y)}`;
            if (tVel) tVel.textContent = `${vel.toFixed(3)} px/ms`;
            if (tAcc) tAcc.textContent = `${acc.toFixed(4)} px/ms²`;
            if (tSteps) tSteps.textContent = `${state.stepCount}`;
            
            state.lastX = x;
            state.lastY = y;
            state.lastTime = now;
            state.lastVel = vel;
            // Persist so the cursor resumes here after a same-origin navigation
            // instead of snapping back to the default start position.
            try { sessionStorage.setItem('__human_cursor_pos__', JSON.stringify({ x: x, y: y })); } catch (e) {}
        };
        
        injectElements();
        document.addEventListener('DOMContentLoaded', injectElements);
        setInterval(injectElements, 200);
    })();
    """
    try:
        page.add_init_script(js)
        page.evaluate(js)  # immediate for current document
    except Exception as e:
        logger.exception("[interaction_primitives] failed to inject synthetic cursor: %s", e)
    # Sync the Python-side cursor tracker to the JS spawn point so the first
    # ballistic move STARTS from the random edge the cursor entered at (otherwise
    # the trajectory would begin at viewport-center and visibly teleport).
    try:
        pos = page.evaluate(
            "() => { const s = window.__human_cursor_state; return s ? {x: s.lastX, y: s.lastY} : null; }"
        )
        if isinstance(pos, dict) and "x" in pos and "y" in pos:
            _set_cursor(page, float(pos["x"]), float(pos["y"]))
    except Exception:
        pass


def _safe_evaluate(page: Any, expression: str, *args: Any) -> Any:
    """Safely evaluate JS on the page with a very low timeout to prevent blocking during navigations."""
    orig_timeout = page._impl.default_timeout if hasattr(page, "_impl") else 25_000
    try:
        page.set_default_timeout(150)  # 150ms max wait for overlay updates
        return page.evaluate(expression, *args)
    except Exception:
        pass
    finally:
        try:
            page.set_default_timeout(orig_timeout)
        except Exception:
            pass


def update_status_ticker(page: Any, action: str, details: str) -> None:
    """No-op: the in-movie status ticker/timeline was removed (see
    ``inject_synthetic_cursor``).

    The action/details are already captured in the SessionRecorder event log,
    which is the intended feed for a post-hoc Loom-style playback tool. Kept as a
    stable no-op so the many call sites (scraper/goal-runner side-trips) need no
    changes, and so it can be re-pointed at the player later.
    """
    return


def enable_visible_mouse_tracking(page: Any) -> None:
    """Inject the synthetic cursor and wire subsequent mouse moves to drive the red overlay.

    Safe to call more than once. The patch on page.mouse.move ensures raw moves
    (calibration paths, some goal steps) also move the visible red cursor.
    Production styled moves go through move_pointer which also drives the cursor
    (see below).
    """
    try:
        inject_synthetic_cursor(page)
    except Exception as e:
        logger.exception("[interaction_primitives] enable_visible_mouse_tracking failed: %s", e)

    try:
        # Patch the raw mouse move so that every single micro-step of the Bezier curve updates the overlay!
        if hasattr(page, "_human_raw_mouse"):
            raw_mouse = page._human_raw_mouse
            orig_raw_move = getattr(raw_mouse, "move", None)
            if orig_raw_move and not getattr(raw_mouse, "_tokyo_raw_cursor_patched", False):
                def _wrapped_raw_move(x: float, y: float, **kw: Any):
                    res = orig_raw_move(x, y, **kw)
                    try:
                        _safe_evaluate(
                            page,
                            "([x,y]) => { const m = window.__human_cursor_move; if (m) m(x,y); }",
                            [int(x), int(y)],
                        )
                    except Exception:
                        pass
                    return res
                raw_mouse.move = _wrapped_raw_move
                try:
                    setattr(raw_mouse, "_tokyo_raw_cursor_patched", True)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        # Patch the mouse.move so that *any* direct page.mouse.move also updates the overlay.
        # This covers calibration sequences in harness/scraper/checker and any other raw moves.
        orig_move = getattr(page.mouse, "move", None)
        if orig_move and not getattr(page.mouse, "_tokyo_cursor_patched", False):
            def _wrapped_move(x: float, y: float, **kw: Any):
                res = orig_move(x, y, **kw)
                try:
                    _safe_evaluate(
                        page,
                        "([x,y]) => { const m = window.__human_cursor_move; if (m) m(x,y); }",
                        [int(x), int(y)],
                    )
                except Exception:
                    pass
                return res
            page.mouse.move = _wrapped_move  # type: ignore[attr-defined]
            try:
                setattr(page.mouse, "_tokyo_cursor_patched", True)
            except Exception:
                pass
    except Exception:
        pass


# Drive the red cursor from the main production move entrypoint too (covers
# all the plan-driven arcs, two-step, imprecision, overshoots, hovers etc.).
# This is the important one for "does the *human* move look good?"
_orig_move_pointer = move_pointer


def move_pointer(
    page: Any,
    x: float,
    y: float,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
    target_w: float | None = None,
    target_h: float | None = None,
) -> dict[str, Any]:
    # Global safety net: a synthetic cursor must never leave the visible viewport.
    # A human cannot move the pointer into rendered-but-unscrolled territory, so an
    # off-screen target (e.g. an element below the fold reported at y > viewport
    # height) is both unnatural and an easy detection signal. We do NOT pin the
    # cursor to the exact viewport rectangle, though: a human's pointer routinely
    # drifts a little past a screen edge (or parks just off-screen and comes
    # back). So we clamp to the viewport EXPANDED by a margin — enough to allow
    # natural edge overflow, but far from the deep rendered-but-unscrolled
    # territory (e.g. y≈2532) that target selection already rules out.
    try:
        vp = page.viewport_size or {}
        vw = int(vp.get("width") or 0)
        vh = int(vp.get("height") or 0)
        if vw > 0 and vh > 0:
            mx = max(40, int(vw * 0.06))
            my = max(40, int(vh * 0.06))
            x = max(-mx, min(vw + mx, x))
            y = max(-my, min(vh + my, y))
    except Exception:
        pass
    res = _orig_move_pointer(page, x, y, plan, recorder=recorder, target_w=target_w, target_h=target_h)
    # Drive the QA synthetic cursor if present (harness / visible Watch / movie).
    # The evaluate is a no-op if the fn is not on window (normal invisible runs).
    try:
        tx = res.get("x", x)
        ty = res.get("y", y)
        _safe_evaluate(
            page,
            "([x,y]) => { const m = window.__human_cursor_move; if (m) m(x,y); }",
            [int(tx), int(ty)],
        )
    except Exception:
        pass
    return res


def _drive_visible_cursor(page: Any, x: float, y: float) -> None:
    """Move the visible (baked) synthetic cursor without a full ballistic move.

    Used for drag traces where we want the red cursor to follow a raw mouse path
    point-by-point instead of jumping. No-op when the overlay isn't injected.
    """
    try:
        _safe_evaluate(
            page,
            "([x,y]) => { const m = window.__human_cursor_move; if (m) m(x,y); }",
            [int(x), int(y)],
        )
    except Exception:
        pass


def trace_text_selection(
    page: Any,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
    select: bool = True,
) -> bool:
    """Trace 1-3 lines of a visible paragraph with the cursor, as a reader does.

    Picks a visible, in-viewport text block, aims the cursor at the start of a
    line, then moves along the text point-by-point for one to three lines so the
    baked cursor visibly follows the sentence with reader-paced dwells.

    ``select`` controls whether this is a *highlight* or a bare *reading trace*:
    - ``select=True`` (default): a genuine selection gesture — press, drag over
      the text, release (real ``mousedown``→``mousemove``→``mouseup``). This is
      the occasional "highlight a sentence" behavior.
    - ``select=False``: the same path WITHOUT the button press, i.e. the common
      "move my eyes/cursor across the line while reading" gesture with no
      selection. Most reading traces should be this.

    Returns True if a trace was performed, False if no suitable text was found.
    """
    rng = _session_rng(page, plan)
    try:
        spot = _safe_evaluate(
            page,
            """() => {
                const vw = innerWidth, vh = innerHeight, HEADER = 64;
                const sels = "article p, article span, main p, [role='article'] p, p, li, .g-feed-post, [class*='text']";
                const els = document.querySelectorAll(sels);
                const cand = [];
                for (let i = 0; i < Math.min(els.length, 100); i++) {
                    const e = els[i];
                    const t = (e.innerText || '').trim();
                    if (t.length < 45) continue;
                    const r = e.getBoundingClientRect();
                    if (r.top < HEADER || r.bottom > vh - 8) continue;
                    if (r.height < 16 || r.width < 140) continue;
                    if (r.left < 6 || r.right > vw - 6) continue;
                    cand.push({left: r.left, top: r.top, w: r.width, h: r.height});
                }
                if (!cand.length) return null;
                return cand[Math.floor(Math.random() * cand.length)];
            }""",
        )
    except Exception:
        spot = None
    if not isinstance(spot, dict):
        return False

    try:
        line_h = 22.0
        max_lines = max(1, int(spot["h"] // line_h))
        n_lines = min(rng.randint(1, 3), max_lines)
        # Start near the left of a line inside the block (not always the top line).
        top_pad = rng.uniform(0.0, max(0.0, spot["h"] - line_h * n_lines))
        start_x = spot["left"] + 3 + rng.uniform(0.0, spot["w"] * 0.08)
        start_y = spot["top"] + top_pad + line_h * 0.6
        end_x = spot["left"] + spot["w"] * rng.uniform(0.45, 0.96)
        end_y = start_y + line_h * (n_lines - 1)

        # Human motor knobs (the same identity-derived physics move_pointer uses), so
        # the sweep is a CURVED, velocity-profiled, tremored drag rather than the old
        # straight constant-velocity interpolation — which read as obviously synthetic
        # motion (no acceleration, no curvature, no tremor) to a kinematics detector.
        hc = _human_config(plan)
        drift = _fatigue(page, plan)
        tremor_hz = _bounded_float(hc.get("tremor_hz"), lower=5.0, upper=14.0, default=10.0)
        tremor_amp = _bounded_float(hc.get("tremor_amp_px"), lower=0.0, upper=3.0, default=0.6) * drift["sloppiness"]
        pl_gain = _bounded_float(hc.get("power_law_gain"), lower=0.0, upper=1.0, default=0.8)
        mj_skew = _bounded_float(hc.get("min_jerk_skew"), lower=0.0, upper=0.45, default=0.12)
        bow = _bounded_float(hc.get("mouse_curve_bow"), lower=0.0, upper=0.5, default=0.14)
        wobble_cap = _bounded_float(hc.get("mouse_wobble_max"), lower=0.0, upper=400.0, default=0.0)
        step_dt = _bounded_float(hc.get("mouse_step_delay_ms"), lower=1.0, upper=50.0, default=8.0)
        clock = [0.0]
        # Reading sweep speed across a line (px/ms): deliberate, slower and more
        # variable than a ballistic acquisition.
        read_speed = rng.uniform(0.55, 1.05)

        def _sweep(p_from: tuple[float, float], p_to: tuple[float, float]) -> None:
            dist = math.hypot(p_to[0] - p_from[0], p_to[1] - p_from[1])
            total_ms = max(150.0, dist / max(0.2, read_speed)) * drift["slowdown"]
            steps = max(8, min(48, int(round(total_ms / max(1.0, step_dt)))))
            p1, p2 = _bezier_controls(p_from, p_to, rng, wobble_cap=wobble_cap, bow=bow)
            _emit_bezier(
                page, p_from, p1, p2, p_to,
                steps=steps, rng=rng, total_ms=total_ms, clock=clock,
                tremor_hz=tremor_hz, tremor_amp_px=tremor_amp,
                power_law_gain=pl_gain, min_jerk_skew=mj_skew,
            )
            _drive_visible_cursor(page, p_to[0], p_to[1])

        # Aim the cursor at the sentence start with a normal ballistic move.
        move_pointer(page, start_x, start_y, plan, recorder=recorder)
        if select:
            # A highlight is one continuous curved drag from start to end.
            page.mouse.down()
            _sweep((start_x, start_y), (end_x, end_y))
            page.mouse.up()
            cur = (end_x, end_y)
        else:
            # A reading trace is a Z-pattern: sweep each line left->right, then a
            # curved "carriage return" down to the next line's start.
            cur = (start_x, start_y)
            for k in range(n_lines):
                ly = start_y + line_h * k
                rx = end_x if k == n_lines - 1 else spot["left"] + spot["w"] * rng.uniform(0.70, 0.96)
                if k > 0:
                    _sweep(cur, (start_x, ly))
                    cur = (start_x, ly)
                _sweep(cur, (rx, ly))
                # Brief end-of-line fixation, as a reader pauses before the next line.
                page.wait_for_timeout(int(lognormal_ms(rng, mean_ms=120.0, cv=0.5, lo=40.0, hi=400.0)))
                cur = (rx, ly)
        _set_cursor(page, cur[0], cur[1])
        if recorder is not None:
            recorder.record(
                "highlight" if select else "reading_trace",
                metadata={"sentences": n_lines, "x": round(cur[0]), "y": round(cur[1])},
            )
        return True
    except Exception:
        logger.debug("[interaction_primitives] text-selection trace failed", exc_info=True)
        if select:
            try:
                page.mouse.up()
            except Exception:
                pass
        return False
