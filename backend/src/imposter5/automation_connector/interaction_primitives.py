"""Small browser interaction primitives for automation connector runs.

These provide the high-level targets and sequences (move to coords with style, position-then-wheel, hover+read, etc.).
The actual human curves/wobbles/bursts/velocities come from the Cloak humanize layer (see cloak_runtime + behavior plan pointer section).
Use move_pointer / scroll_page etc (or _safe_mouse_move for robustness); never raw page.mouse.* in new code so that plan variance + recording + styled physics are always applied.
"""
from __future__ import annotations

import random
import string
from typing import Any

from imposter5.automation_connector.behavior_policy import (
    planned_scroll_delta,
    planned_wait_ms,
)
from imposter5.automation_connector.session_recorder import SessionRecorder


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
    update_status_ticker(page, "⏳ WAITING / READING", f"Pause: {wait_ms}ms (pass {pass_index})")
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
    if plan and plan.get("persona", {}).get("name") == "naive_bot":
        page.mouse.wheel(0, delta_y)
        if recorder is not None:
            recorder.record("scroll", metadata={"pass_index": pass_index, "delta_y": delta_y})
        return delta_y

    delta_y = planned_scroll_delta(plan, pass_index, fallback_delta_y)
    rng = _seeded_rng(plan, f"scroll:{pass_index}")
    update_status_ticker(page, "📜 SCROLLING", f"Delta: {delta_y}px (pass {pass_index})")
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
    if plan and plan.get("persona", {}).get("name") == "naive_bot":
        locator.fill(text)
        result = {"typed_chars": len(text), "typos": 0, "corrections": 0}
        if recorder is not None:
            recorder.record("type_text", metadata={"selector": selector, **result})
        return result

    update_status_ticker(page, "⌨️ TYPING", f"Input: {selector} - '{text[:20]}...'")
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
    selector: Any,
    plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Click an element with optional pre-click hover."""
    if plan and plan.get("persona", {}).get("name") == "naive_bot":
        try:
            if isinstance(selector, str):
                page.locator(selector).click()
            else:
                selector.click()
        except Exception:
            pass
        result = {
            "hovered": False,
            "move_style": "direct",
        }
        if recorder is not None:
            recorder.record("click", metadata={"selector": str(selector), **result})
        return result

    pointer = _pointer_plan(plan)
    rng = _seeded_rng(plan, f"click:{selector}")
    
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

            if valid_links:
                locator, box = rng.choice(valid_links)
                cx = box["x"] + box["width"] * rng.uniform(0.28, 0.72)
                cy = box["y"] + box["height"] * rng.uniform(0.28, 0.72)
                update_status_ticker(page, "🖱️ CLICKING", f"Clicking: random link at ({round(cx)}, {round(cy)})")
                move_meta = move_pointer(page, cx, cy, plan, recorder=recorder)
                locator.click()
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
                page.mouse.click(cx, cy)
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
            page.mouse.click(cx, cy)
            return {"clicked_fallback_spot": True}

    if isinstance(selector, str):
        # --- Stateful Honeypot Evasion (Layer 4 Defense) ---
        # Before clicking, check if the selector points to a hidden / honeypot element.
        # Honeypots are styled to be invisible to humans but are visible in the DOM.
        try:
            loc = page.locator(selector)
            is_honeypot = page.evaluate("""
                (sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    
                    // 1. Standard hidden styles
                    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
                        return true;
                    }
                    
                    // 2. Off-screen positioning (common honeypot technique)
                    const rect = el.getBoundingClientRect();
                    if (rect.right < 0 || rect.bottom < 0 || rect.left > window.innerWidth || rect.top > window.innerHeight) {
                        return true;
                    }
                    
                    // 3. Zero dimensions
                    if (rect.width === 0 || rect.height === 0) {
                        return true;
                    }
                    
                    // 4. Hidden by absolute positioning/z-index or tiny font size
                    if (parseFloat(style.fontSize) === 0 || parseInt(style.zIndex) < -1000) {
                        return true;
                    }
                    
                    return false;
                }
            """, selector)
            
            if is_honeypot:
                update_status_ticker(page, "⚠️ EVADING HONEYPOT", f"Detected hidden honeypot: {selector}. Bypassing click.")
                if recorder is not None:
                    recorder.record("honeypot_evaded", metadata={"selector": selector})
                return {"hovered": False, "honeypot_evaded": True}
        except Exception as e:
            pass

        locator = page.locator(selector)
        update_status_ticker(page, "🖱️ CLICKING", f"Clicking: {selector}")
    else:
        locator = selector
        update_status_ticker(page, "🖱️ CLICKING", "Clicking element handle")

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
    if plan and plan.get("persona", {}).get("name") == "naive_bot":
        try:
            if isinstance(selector, str):
                page.locator(selector).hover()
            else:
                selector.hover()
        except Exception:
            pass
        result = {
            "hovered": True,
        }
        if recorder is not None:
            recorder.record("hover", metadata={"selector": str(selector), **result})
        return result

    hover = plan.get("hover") if isinstance(plan, dict) else {}
    hover = hover if isinstance(hover, dict) else {}
    dwell_ms = _bounded_int(hover.get("hover_dwell_ms"), lower=150, upper=1_500, default=450)
    
    if isinstance(selector, str):
        locator = page.locator(selector)
        update_status_ticker(page, "👁️ HOVERING", f"Hovering: {selector} (dwell {dwell_ms}ms)")
    else:
        locator = selector
        update_status_ticker(page, "👁️ HOVERING", f"Hovering element handle (dwell {dwell_ms}ms)")

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
    if plan and plan.get("persona", {}).get("name") == "naive_bot":
        page.mouse.move(x, y)
        res = {
            "x": round(x),
            "y": round(y),
            "style": "direct",
            "imprecision_px": 0,
            "overshot": False,
        }
        if recorder is not None:
            recorder.record("mouse_move", metadata=res)
        return res

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
    pages + direct evaluate for the current document. Features high-precision 
    canvas drawing for color-coded velocity/acceleration trails and real-time
    telemetry overlay.
    """
    js = """
    (function(){
        // Store state persistently on window so it survives re-injection
        if (!window.__human_cursor_state) {
            window.__human_cursor_state = {
                points: [],
                lastX: 220,
                lastY: 220,
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
            if (!canvas) {
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
                cursor.innerHTML = '<div class="arrow"></div><div class="dot"></div><div class="label">HUMAN MOUSE</div>';
                document.documentElement.appendChild(cursor);
                cursor.style.left = (state.lastX | 0) + 'px';
                cursor.style.top = (state.lastY | 0) + 'px';
            }
            
            let tel = document.getElementById('__human_cursor_telemetry__');
            if (!tel) {
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
            if (!ticker) {
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
            
            state.points.push({
                x: x,
                y: y,
                timestamp: now,
                vel: vel,
                acc: acc
            });
            
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
    """Update the futuristic bottom status ticker with the current active behavior."""
    try:
        _safe_evaluate(
            page,
            "([action, details]) => { "
            "  if (window.__human_cursor_state) { "
            "    window.__human_cursor_state.currentAction = action; "
            "    window.__human_cursor_state.currentDetails = details; "
            "    const timeStr = new Date().toTimeString().split(' ')[0]; "
            "    window.__human_cursor_state.timelineEvents.push({ time: timeStr, action: action, details: details }); "
            "    while (window.__human_cursor_state.timelineEvents.length > 5) { "
            "      window.__human_cursor_state.timelineEvents.shift(); "
            "    } "
            "  } "
            "  const actEl = document.getElementById('__ticker_current_action__'); "
            "  const detEl = document.getElementById('__ticker_details__'); "
            "  const tlEl = document.getElementById('__ticker_timeline__'); "
            "  if (actEl) actEl.textContent = action; "
            "  if (detEl) detEl.textContent = details; "
            "  if (tlEl) { "
            "    const timeStr = new Date().toTimeString().split(' ')[0]; "
            "    const item = document.createElement('div'); "
            "    item.style.cssText = 'color: #cbd5e1 !important; font-size: 10px !important; display: flex !important; gap: 6px !important;'; "
            "    item.innerHTML = `<span style=\"color: #64748b !important;\">[${timeStr}]</span> <span style=\"color: #f43f5e !important; font-weight: bold !important;\">${action}</span> <span>${details}</span>`; "
            "    tlEl.appendChild(item); "
            "    while (tlEl.children.length > 5) { tlEl.removeChild(tlEl.firstChild); } "
            "  } "
            "}",
            [action, details]
        )
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
) -> dict[str, Any]:
    res = _orig_move_pointer(page, x, y, plan, recorder=recorder)
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
