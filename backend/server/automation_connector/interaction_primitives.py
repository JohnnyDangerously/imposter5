"""Small browser interaction primitives for automation connector runs.

These provide the high-level targets and sequences (move to coords with style, position-then-wheel, hover+read, etc.).
The actual human curves/wobbles/bursts/velocities come from the Cloak humanize layer (see cloak_runtime + behavior plan pointer section).
Use move_pointer / scroll_page etc (or _safe_mouse_move for robustness); never raw page.mouse.* in new code so that plan variance + recording + styled physics are always applied.
"""
from __future__ import annotations

import random
import string
from typing import Any

from server.automation_connector.behavior_policy import (
    planned_scroll_delta,
    planned_wait_ms,
)
from server.automation_connector.session_recorder import SessionRecorder


def _seeded_rng(plan: dict[str, Any] | None, namespace: str) -> random.Random:
    seed = ""
    if isinstance(plan, dict):
        seed = str(plan.get("run_id") or "")
    return random.Random(f"{seed}:{namespace}")


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
    page.wait_for_timeout(wait_ms)
    if recorder is not None:
        recorder.record("wait", metadata={"pass_index": pass_index, "wait_ms": wait_ms})
    return wait_ms


def scroll_page(
    page: Any,
    plan: dict[str, Any] | None,
    pass_index: int = 0,
    fallback_delta_y: int = 900,
    *,
    recorder: SessionRecorder | None = None,
) -> int:
    """Scroll using a planned bounded delta, with mouse positioning over content first so the wheel is accompanied by realistic mouse events (for detectors and human twin traces). Returns the actual delta used."""
    delta_y = planned_scroll_delta(plan, pass_index, fallback_delta_y)
    rng = _seeded_rng(plan, f"scroll:{pass_index}")
    # Position the mouse over a content area before the wheel so the scroll "event" has an associated cursor position (mouse scroll realism, not just raw wheel from nowhere).
    _position_mouse_over_content(page, plan, rng, recorder)
    page.mouse.wheel(0, delta_y)
    # Small post-scroll mouse adjustment (simulates eyes/hand following the content after scroll).
    if rng.random() < 0.4:
        _micro_mouse_adjust(page, plan, rng, recorder)
    if recorder is not None:
        recorder.record("scroll", metadata={"pass_index": pass_index, "delta_y": delta_y})
    return delta_y


def _position_mouse_over_content(page: Any, plan: dict[str, Any] | None, rng: random.Random, recorder: SessionRecorder | None = None) -> None:
    """Best-effort move mouse to a plausible reading/viewport content spot before scroll or interaction."""
    try:
        for sel in ("main", "article", ".feed-shared-update-v2", "section[aria-label*='feed' i]", "[role='main']", "body"):
            loc = page.locator(sel).first
            b = loc.bounding_box() if loc else None
            if b and b.get("width", 0) > 80:
                x = b["x"] + b["width"] * rng.uniform(0.28, 0.62)
                y = b["y"] + b["height"] * rng.uniform(0.32, 0.72)
                _safe_mouse_move(page, x, y, plan, rng, recorder=recorder)
                return
    except Exception:
        pass
    # Fallback rough content area move (still better than no mouse move). Uses _safe so we
    # get plan styles + recording even on partial failures (preserves movement quality).
    try:
        _safe_mouse_move(page, 320 + rng.uniform(-40, 40), 380 + rng.uniform(-60, 60), plan, rng, recorder=recorder)
    except Exception:
        pass


def _micro_mouse_adjust(page: Any, plan: dict[str, Any] | None, rng: random.Random, recorder: SessionRecorder | None = None) -> None:
    """Tiny mouse move to simulate following content after a scroll or during reading.
    Uses _safe_mouse_move so even in exception paths we prefer styled plan-driven moves (and record them)
    for consistent human-like mouse events.
    """
    try:
        _safe_mouse_move(page, 340 + rng.uniform(-12, 12), 420 + rng.uniform(-18, 18), plan, rng, recorder=recorder)
    except Exception:
        pass


def _neighboring_char(char: str) -> str:
    alphabet = string.ascii_lowercase
    lower = char.lower()
    if lower not in alphabet:
        return char
    idx = alphabet.index(lower)
    replacement = alphabet[min(len(alphabet) - 1, idx + 1)]
    return replacement.upper() if char.isupper() else replacement


def type_text(
    page: Any,
    selector: str,
    text: str,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Type text into a selector with bounded delays and occasional corrections."""
    typing_plan = plan.get("typing") if isinstance(plan, dict) else {}
    typing_plan = typing_plan if isinstance(typing_plan, dict) else {}
    min_delay = int(typing_plan.get("min_delay_ms", 55))
    max_delay = max(min_delay, int(typing_plan.get("max_delay_ms", 170)))
    typo_chance = float(typing_plan.get("typo_chance", 0.0))
    correction_chance = float(typing_plan.get("correction_chance", 1.0))
    pause_chance = float(typing_plan.get("pause_mid_query_chance", 0.0))
    rng = _seeded_rng(plan, f"type:{selector}:{text}")

    locator = page.locator(selector)
    typed = 0
    typos = 0
    corrections = 0
    locator.click()
    for index, char in enumerate(text):
        if rng.random() < pause_chance and index > 0:
            page.wait_for_timeout(rng.randint(250, 900))
        if typo_chance > 0 and char.strip() and rng.random() < typo_chance:
            locator.type(_neighboring_char(char), delay=rng.randint(min_delay, max_delay))
            typos += 1
            if rng.random() < correction_chance:
                locator.press("Backspace")
                corrections += 1
        locator.type(char, delay=rng.randint(min_delay, max_delay))
        typed += 1
    result = {"typed_chars": typed, "typos": typos, "corrections": corrections}
    if recorder is not None:
        recorder.record("type_text", metadata={"selector": selector, **result})
    return result


def click_element(
    page: Any,
    selector: str,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Click an element with optional pre-click hover."""
    pointer = _pointer_plan(plan)
    rng = _seeded_rng(plan, f"click:{selector}")
    locator = page.locator(selector)
    hovered = False
    move_meta: dict[str, Any] | None = None
    if rng.random() < float(pointer.get("hover_before_click_chance", 0.0)):
        locator.hover()
        hovered = True
    else:
        try:
            box = locator.bounding_box()
            if box:
                cx = box["x"] + box["width"] * rng.uniform(0.28, 0.72)
                cy = box["y"] + box["height"] * rng.uniform(0.28, 0.72)
                move_meta = move_pointer(page, cx, cy, plan, recorder=recorder)
        except Exception:
            pass
    locator.click()
    result = {
        "hovered": hovered,
        "move_style": (move_meta or {}).get("style") or pointer.get("move_style", "direct"),
    }
    if move_meta:
        result["move"] = move_meta
    if recorder is not None:
        recorder.record("click", metadata={"selector": selector, **result})
    return result


def hover_element(
    page: Any,
    selector: str,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Hover an element and dwell for a bounded interval."""
    hover = plan.get("hover") if isinstance(plan, dict) else {}
    hover = hover if isinstance(hover, dict) else {}
    dwell_ms = _bounded_int(hover.get("hover_dwell_ms"), lower=150, upper=1_500, default=450)
    locator = page.locator(selector)
    try:
        box = locator.bounding_box()
        if box:
            hx = box["x"] + box["width"] * _seeded_rng(plan, f"hover:{selector}").uniform(0.25, 0.75)
            hy = box["y"] + box["height"] * _seeded_rng(plan, f"hover:{selector}").uniform(0.25, 0.75)
            move_pointer(page, hx, hy, plan, recorder=recorder)
    except Exception:
        pass
    locator.hover()
    page.wait_for_timeout(dwell_ms)
    result = {"selector": selector, "hover_dwell_ms": dwell_ms}
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
    rng = _seeded_rng(plan, "expand_comments")
    clicked = 0
    attempted = 0
    for selector in selectors:
        if clicked >= max_expansions:
            break
        attempted += 1
        if rng.random() > chance:
            continue
        try:
            page.locator(selector).click()
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
    should_backtrack = max_backtracks > 0 and _seeded_rng(plan, "backtrack").random() < chance
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


def move_pointer(
    page: Any,
    x: float,
    y: float,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Move cursor to coordinates honoring the plan's pointer.move_style, imprecision, and overshoot."""
    pointer = _pointer_plan(plan)
    style = str(pointer.get("move_style") or "direct")
    imprec = _bounded_int(pointer.get("imprecision_px"), lower=0, upper=20, default=3)
    ovr_ch = _bounded_float(pointer.get("overshoot_chance"), lower=0.0, upper=0.5, default=0.04)
    rng = _seeded_rng(plan, f"ptr:{round(x)}:{round(y)}")

    tx = float(x) + rng.uniform(-imprec, imprec)
    ty = float(y) + rng.uniform(-imprec, imprec)

    segments: list[tuple[float, float]] = []
    used = style
    if style == "slight_arc":
        off = rng.uniform(18, 48) * (1 if rng.random() < 0.5 else -1)
        segments.append((tx + off * 0.7, ty - 18))
        segments.append((tx, ty))
        used = "slight_arc"
    elif style == "two_step":
        offx = rng.uniform(10, 32) * (1 if rng.random() < 0.5 else -1)
        offy = rng.uniform(6, 22) * (1 if rng.random() < 0.5 else -1)
        segments.append((tx + offx, ty + offy))
        segments.append((tx, ty))
        used = "two_step"
    else:
        segments.append((tx, ty))
        used = "direct"

    for sx, sy in segments:
        page.mouse.move(sx, sy)
        if len(segments) > 1:
            page.wait_for_timeout(rng.randint(6, 22))

    overshot = False
    if rng.random() < ovr_ch:
        ox = tx + rng.uniform(3, 11) * (1 if rng.random() < 0.5 else -1)
        oy = ty + rng.uniform(2, 7) * (1 if rng.random() < 0.5 else -1)
        page.mouse.move(ox, oy)
        page.wait_for_timeout(rng.randint(18, 55))
        page.mouse.move(tx + rng.uniform(-0.9, 0.9), ty + rng.uniform(-0.9, 0.9))
        overshot = True

    res = {
        "x": round(tx),
        "y": round(ty),
        "style": used,
        "imprecision_px": imprec,
        "overshot": overshot,
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
    pages + direct evaluate for the current document. Parks at (220,220) so it
    is immediately obvious even before the first real move.
    """
    js = """
    (function(){
        if (document.getElementById('__human_cursor__')) return;
        const style = document.createElement('style');
        style.id = '__human_cursor_style__';
        style.textContent = `
            #__human_cursor__ {
                position: fixed !important;
                left: 0; top: 0;
                width: 56px; height: 56px;
                pointer-events: none !important;
                z-index: 2147483647 !important;
                transform: translate(-50%, -50%);
                filter: drop-shadow(0 2px 4px rgba(0,0,0,0.6));
            }
            #__human_cursor__ .arrow {
                position: absolute;
                left: 8px; top: 8px;
                width: 0; height: 0;
                border-left: 18px solid transparent;
                border-right: 18px solid transparent;
                border-bottom: 28px solid #ff1a1a;
            }
            #__human_cursor__ .dot {
                position: absolute;
                left: 22px; top: 22px;
                width: 12px; height: 12px;
                background: #fff;
                border: 3px solid #ff1a1a;
                border-radius: 999px;
            }
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
        `;
        document.documentElement.appendChild(style);
        const c = document.createElement('div');
        c.id = '__human_cursor__';
        c.setAttribute('aria-hidden', 'true');
        c.innerHTML = '<div class="arrow"></div><div class="dot"></div><div class="label">HUMAN MOUSE</div>';
        document.documentElement.appendChild(c);
        window.__human_cursor_move = function(x, y) {
            const el = document.getElementById('__human_cursor__');
            if (el) {
                el.style.left = (x | 0) + 'px';
                el.style.top = (y | 0) + 'px';
            }
        };
        // Park visibly so even with no moves yet the overlay is obvious on screen.
        if (window.__human_cursor_move) {
            window.__human_cursor_move(220, 220);
        }
        try { console.log('[human-cursor] large bright overlay active for visual QA only'); } catch(e){}
    })();
    """
    try:
        page.add_init_script(js)
        page.evaluate(js)  # immediate for current document
    except Exception:
        pass


def enable_visible_mouse_tracking(page: Any) -> None:
    """Inject the synthetic cursor and wire subsequent mouse moves to drive the red overlay.

    Safe to call more than once. The patch on page.mouse.move ensures raw moves
    (calibration paths, some goal steps) also move the visible red cursor.
    Production styled moves go through move_pointer which also drives the cursor
    (see below).
    """
    try:
        inject_synthetic_cursor(page)
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
                    page.evaluate(
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
) -> dict[str, Any]:
    res = _orig_move_pointer(page, x, y, plan, recorder=recorder)
    # Drive the QA synthetic cursor if present (harness / visible Watch / movie).
    # The evaluate is a no-op if the fn is not on window (normal invisible runs).
    try:
        tx = res.get("x", x)
        ty = res.get("y", y)
        page.evaluate(
            "([x,y]) => { const m = window.__human_cursor_move; if (m) m(x,y); }",
            [int(tx), int(ty)],
        )
    except Exception:
        pass
    return res
