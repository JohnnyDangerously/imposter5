from __future__ import annotations

from typing import Any

from imposter5.automation_connector.interaction_primitives import (
    click_element,
    hover_element,
    maybe_backtrack,
    maybe_expand_comments,
    mobile_swipe,
    move_pointer,
    scroll_page,
    type_text,
    wait_human,
)
from imposter5.automation_connector.session_recorder import SessionRecorder


class FakeMouse:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events

    def wheel(self, delta_x: int, delta_y: int) -> None:
        self.events.append(("wheel", {"delta_x": delta_x, "delta_y": delta_y}))

    def move(self, x: float, y: float) -> None:
        self.events.append(("mouse_move", {"x": round(x), "y": round(y)}))

    def click(self, x: float, y: float, *, delay: int = 0) -> None:
        # Real Playwright Mouse.click(x, y, delay=) presses at the cursor's current
        # (realized) point and holds the button for ``delay`` ms; the humanized click
        # uses this instead of locator.click() so Playwright never recenters to the
        # element middle, and passes a non-zero hold so down!=up timestamps.
        self.events.append(("mouse_click", {"x": round(x), "y": round(y), "delay": int(delay)}))

    def down(self) -> None:
        self.events.append(("mouse_down", None))

    def up(self) -> None:
        self.events.append(("mouse_up", None))


class FakeLocator:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events

    @property
    def first(self) -> "FakeLocator":
        # Real Playwright locators expose ``.first``; the humanized primitives
        # narrow string selectors to it before acting / honeypot-probing.
        return self

    def evaluate(self, script: str) -> str:
        # Honeypot trap probe runs el-scoped JS; the fake element is never a trap.
        return ""

    def click(self, *, delay: int = 0) -> None:
        # Real Playwright Locator.click(delay=) holds the button for ``delay`` ms.
        self.events.append(("locator_click", None))

    def hover(self) -> None:
        self.events.append(("hover", None))

    def bounding_box(self) -> dict[str, float]:
        # Return a stable box so move_pointer pre-positioning code paths execute in tests.
        return {"x": 100.0, "y": 100.0, "width": 120.0, "height": 30.0}

    def press(self, key: str) -> None:
        self.events.append(("press", key))

    def type(self, text: str, *, delay: int) -> None:
        self.events.append(("type", {"text": text, "delay": delay}))


class FakeKeyboard:
    """Stand-in for ``page.keyboard``. The humanized (digraph-mode) ``type_text``
    drives REAL key events through this — ``press(key, delay=hold)`` is a
    keydown -> held -> keyup with a measurable press/release dwell — instead of the
    0 ms ``locator.type()`` insert that any keystroke-dynamics detector flags."""

    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events

    def press(self, key: str, *, delay: int = 0) -> None:
        self.events.append(("key_press", {"key": key, "delay": int(delay)}))

    def down(self, key: str) -> None:
        self.events.append(("key_down", key))

    def up(self, key: str) -> None:
        self.events.append(("key_up", key))

    def type(self, text: str, *, delay: int = 0) -> None:
        self.events.append(("key_type", {"text": text, "delay": int(delay)}))


class FakeElementHandle:
    """Stand-in for an ElementHandle returned by ``page.query_selector`` — its
    ``bounding_box`` returns immediately (no auto-wait), mirroring the fast-fail
    content probe used before scrolling."""

    def bounding_box(self) -> dict[str, float]:
        # Content-area sized box (a real ``main`` is tall), so the pre-scroll
        # mouse positioning resolves a real target.
        return {"x": 200.0, "y": 120.0, "width": 700.0, "height": 600.0}


class FakePage:
    # Humanized aimed movement (Fitts/min-jerk) reads viewport_size to seed the
    # initial cursor position; provide it so the double matches Playwright.
    viewport_size = {"width": 1280, "height": 800}

    default_timeout = 25_000

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.mouse = FakeMouse(self.events)
        self.keyboard = FakeKeyboard(self.events)

    def set_default_timeout(self, timeout_ms: int) -> None:
        self.default_timeout = timeout_ms

    def evaluate(self, expression: str, *args: Any) -> Any:
        # Pre-scroll mouse positioning now probes for a VISIBLE content rect via a
        # single JS evaluate (intersecting containers with the viewport) instead of
        # query_selector + bounding_box, so the cursor lands on-screen and the wheel
        # actually dispatches a DOM event. The probe returns ``{rects, vw, vh,
        # header}`` (a list of on-screen rects plus viewport dims); return that shape
        # so positioning resolves a target and reliably emits a mouse move.
        self.events.append(("evaluate", expression))
        return {
            "rects": [{"left": 200.0, "top": 120.0, "w": 700.0, "h": 600.0}],
            "vw": 1280.0,
            "vh": 800.0,
            "header": 64.0,
        }

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.events.append(("wait", timeout_ms))

    def locator(self, selector: str) -> FakeLocator:
        self.events.append(("locator", selector))
        return FakeLocator(self.events)

    def query_selector(self, selector: str) -> "FakeElementHandle | None":
        # Fast-fail content probe (no auto-wait). First content selector resolves.
        self.events.append(("query_selector", selector))
        return FakeElementHandle()

    def go_back(self, *, wait_until: str) -> None:
        self.events.append(("go_back", wait_until))


def test_wait_and_scroll_use_planned_values() -> None:
    page = FakePage()
    # This test asserts the planned-delta momentum burst (all-positive, sums to the
    # plan). The reverse-direction overshoot "debounce" is a separate, stochastic
    # feature (default 22% chance, has its own test) seeded from per-session entropy
    # when no ``session_seed`` is set — so leave it on here and this test flips ~1-in-5
    # runs. Disable it for this case so the planned-value invariant is deterministic.
    plan = {
        "run_id": "r1",
        "pacing": {"wait_ms": [321], "scroll_delta_y": [654]},
        "human_config": {"scroll_overshoot_chance": 0.0},
    }

    assert wait_human(page, plan, 0, 800) == 321
    assert scroll_page(page, plan, 0, 900) == 654

    # Positioning for realistic mouse+wheel (the quality improvement) now probes
    # for a VISIBLE content rect via a single fast JS evaluate (intersecting
    # containers with the viewport), then moves the cursor there (via move_pointer)
    # before the wheel — so the wheel has a real on-screen origin and dispatches a
    # DOM wheel event. We assert the planned values are honored and that the
    # human-like mouse events for scroll happen.
    evs = page.events
    assert ("wait", 321) in evs
    # Scroll now models momentum bleed-off: a decaying burst of wheel events whose
    # signed deltas sum to (approximately) the planned 654px, not one raw wheel.
    wheels = [e[1]["delta_y"] for e in evs if e[0] == "wheel"]
    assert len(wheels) >= 2, "scroll should emit a multi-step decaying wheel burst"
    assert all(d > 0 for d in wheels), "downward scroll => all-positive wheel deltas"
    assert abs(sum(wheels) - 654) <= 2, "decaying burst should sum to the planned delta"
    assert any(e[0] == "evaluate" for e in evs), "scroll should probe (via evaluate) for a visible content rect to position the mouse"
    assert any(e[0] == "mouse_move" for e in evs), "scroll should produce mouse moves for realistic scroll events"


def test_type_text_uses_locator_and_returns_metadata() -> None:
    page = FakePage()
    plan = {
        "run_id": "r2",
        "typing": {
            "min_delay_ms": 1,
            "max_delay_ms": 1,
            "typo_chance": 0,
            "correction_chance": 1,
            "pause_mid_query_chance": 0,
        },
    }

    result = type_text(page, "input[name='q']", "abc", plan)

    assert result == {"typed_chars": 3, "typos": 0, "corrections": 0}
    assert ("locator", "input[name='q']") in page.events
    assert page.events.count(("type", {"text": "a", "delay": 1})) == 1
    assert page.events.count(("type", {"text": "b", "delay": 1})) == 1
    assert page.events.count(("type", {"text": "c", "delay": 1})) == 1


def _digraph_typing_plan(*, seed: str = "kbd", typo: float = 0.0, correction: float = 1.0, pause: float = 0.0) -> dict:
    """A modern (digraph-mode) typing plan: carries ``base_interkey_ms``/``key_hold_ms``
    so ``type_text`` drives real key events with a genuine press/release dwell."""
    return {
        "session_seed": seed,
        "typing": {
            "base_interkey_ms": 140.0,
            "interkey_cv": 0.30,
            "key_hold_ms": 80.0,
            "typo_chance": typo,
            "correction_chance": correction,
            "pause_mid_query_chance": pause,
        },
    }


def test_type_text_digraph_drives_real_key_events_with_dwell() -> None:
    page = FakePage()
    result = type_text(page, "input[name='q']", "hello", _digraph_typing_plan())

    presses = [m for a, m in page.events if a == "key_press"]
    # One REAL keystroke per character, in order, via page.keyboard (not locator.type).
    assert [p["key"] for p in presses] == list("hello")
    assert all(a != "type" for a, _ in page.events), "digraph mode must not fall back to locator.type for ASCII"
    # Every keystroke carries a measurable press->release DWELL — the signal a 0 ms
    # locator.type() can never produce (the keystroke-dynamics dead giveaway).
    assert all(p["delay"] >= 18 for p in presses)
    mean_dwell = sum(p["delay"] for p in presses) / len(presses)
    assert 30.0 <= mean_dwell <= 200.0
    # Inter-key flight gaps were waited between keystrokes.
    assert sum(1 for a, _ in page.events if a == "wait") >= 4
    assert result == {"typed_chars": 5, "typos": 0, "corrections": 0}


def test_type_text_digraph_makes_and_corrects_varied_mistakes() -> None:
    # typo_chance=1 forces an error on every content char; correction_chance=1 means
    # each is noticed and fixed. The final committed text must still be correct.
    page = FakePage()
    result = type_text(page, "input[name='q']", "research", _digraph_typing_plan(seed="oops", typo=1.0, correction=1.0))

    assert result["typos"] >= 1
    assert result["corrections"] >= 1
    keys = [m["key"] for a, m in page.events if a == "key_press"]
    assert "Backspace" in keys, "a corrected mistake must emit real Backspace key events"
    # Replay the key stream (presses + backspaces) and prove it lands on the intended
    # word — i.e. the varied mistake/correction logic always converges to correct text.
    buf: list[str] = []
    for k in keys:
        if k == "Backspace":
            if buf:
                buf.pop()
        else:
            buf.append(" " if k == "Space" else k)
    assert "".join(buf) == "research"


def test_type_text_digraph_can_leave_some_mistakes_uncorrected() -> None:
    # With correction_chance=0 the typist never fixes errors: typos are recorded but
    # no Backspace is emitted (honest uncorrected error path).
    page = FakePage()
    result = type_text(page, "input[name='q']", "engineer", _digraph_typing_plan(seed="raw", typo=1.0, correction=0.0))
    keys = [m["key"] for a, m in page.events if a == "key_press"]
    assert result["typos"] >= 1
    assert result["corrections"] == 0
    assert "Backspace" not in keys


def test_type_text_digraph_is_deterministic_per_seed() -> None:
    p1 = FakePage()
    p2 = FakePage()
    type_text(p1, "i", "research scientist", _digraph_typing_plan(seed="det", typo=0.5))
    type_text(p2, "i", "research scientist", _digraph_typing_plan(seed="det", typo=0.5))
    # Same session seed => identical keystroke stream (reproducible), while a different
    # seed would diverge — randomness is per-session, not a fixed pre-baked pattern.
    assert p1.events == p2.events
    p3 = FakePage()
    type_text(p3, "i", "research scientist", _digraph_typing_plan(seed="other", typo=0.5))
    assert p3.events != p1.events


def test_click_element_can_hover_first() -> None:
    page = FakePage()
    plan = {
        "run_id": "r3",
        "pointer": {"hover_before_click_chance": 1, "move_style": "two_step"},
    }

    result = click_element(page, "button", plan)

    # Hover-before-click is now a human settle pause on the target plus a real
    # cursor approach, then a click at the realized landing point — never a native
    # locator.hover()/locator.click() center-snap.
    assert result["hovered"] is True
    assert result["move_style"] in {"ballistic", "ballistic_correct", "ballistic_overshoot_correct"}
    assert any(e[0] == "mouse_move" for e in page.events)
    assert any(e[0] == "mouse_click" for e in page.events)
    assert ("hover", None) not in page.events
    assert ("locator_click", None) not in page.events


def test_click_holds_the_button_for_a_human_dwell() -> None:
    page = FakePage()
    click_element(page, "button.save", {"run_id": "hold", "pointer": {"hover_before_click_chance": 0}})
    clicks = [m for a, m in page.events if a == "mouse_click"]
    assert clicks, "expected a realized-point mouse click"
    # A real click holds the button ~50-120 ms; never a 0 ms down==up timestamp.
    assert all(c["delay"] >= 30 for c in clicks)
    assert all(c["delay"] <= 200 for c in clicks)


def test_hover_expand_and_mobile_primitives_record_metadata() -> None:
    page = FakePage()
    plan = {
        "run_id": "r4",
        "hover": {"hover_dwell_ms": 200, "expand_comments_chance": 1, "max_expansions": 1},
        "mobile": {"enabled": True, "gesture_style": "short_swipe", "max_swipes": 1},
        "pacing": {"scroll_delta_y": [444]},
        "recorder": {"enabled": True, "max_events": 10},
    }
    recorder = SessionRecorder(plan)

    hover_result = hover_element(page, "a.person", plan, recorder=recorder)
    expand_result = maybe_expand_comments(page, ("button.comments", "button.more"), plan, recorder=recorder)
    swipe_result = mobile_swipe(page, plan, recorder=recorder)

    assert hover_result == {"selector": "a.person", "hover_dwell_ms": 200}
    assert swipe_result == {"swiped": True, "delta_y": 444, "gesture_style": "short_swipe"}
    # Comment expansion is now a human-realism coin flip (chance clamps to <=0.35),
    # so assert the bounded structure rather than a guaranteed expansion.
    assert expand_result["max_expansions"] == 1
    assert 1 <= expand_result["attempted"] <= 2
    assert 0 <= expand_result["expanded"] <= expand_result["attempted"]
    assert expand_result["expanded"] <= expand_result["max_expansions"]
    # Hover + mobile swipe always record; expansion may add one more.
    assert recorder.payload()["event_count"] >= 2


def test_backtrack_is_bounded_and_optional() -> None:
    page = FakePage()
    plan = {
        "run_id": "r5",
        "backtracking": {"micro_abandon_chance": 0, "max_backtracks": 1},
    }

    result = maybe_backtrack(page, plan)

    assert result == {"backtracked": False, "max_backtracks": 1}
    assert ("go_back", "domcontentloaded") not in page.events


def test_move_pointer_emits_human_aimed_movement() -> None:
    page = FakePage()
    plan = {
        "run_id": "rm1",
        "pointer": {"move_style": "two_step", "imprecision_px": 0, "overshoot_chance": 0},
    }

    res = move_pointer(page, 640, 360, plan)
    # The humanized model reports the realized aimed-movement phase (Fitts duration +
    # minimum-jerk Bezier + corrective submovements), not the old cosmetic move_style.
    assert res["style"] in {"ballistic", "ballistic_correct", "ballistic_overshoot_correct"}
    assert res["overshot"] is False  # overshoot_chance 0 => deterministically no overshoot
    assert res["steps"] >= 8
    assert res["submovements"] >= 1
    # A real aimed movement is many micro-steps, not a fixed 2.
    assert len([e for e in page.events if e[0] == "mouse_move"]) >= 8
    # imprecision 0 => the cursor ends exactly on the requested target.
    assert (res["x"], res["y"]) == (640, 360)

    # Overshoot is probabilistic and physiologically capped (chance clamps to <=0.5),
    # so drive several seeds to deterministically exercise the corrective-return path.
    overshot_res = None
    for i in range(40):
        p = FakePage()
        ri = move_pointer(p, 900, 600, {"run_id": f"ovr-{i}", "pointer": {"imprecision_px": 0, "overshoot_chance": 1.0}})
        if ri["overshot"]:
            overshot_res = ri
            break
    assert overshot_res is not None, "overshoot path should fire within a few dozen seeds at max chance"
    assert overshot_res["style"] == "ballistic_overshoot_correct"
    assert overshot_res["submovements"] == 2


def test_click_and_hover_drive_pointer_moves() -> None:
    page = FakePage()
    plan = {
        "run_id": "rc",
        "pointer": {"hover_before_click_chance": 0, "move_style": "direct"},
    }
    click_element(page, "button.save", plan)
    hover_element(page, "a.link", plan)
    moves = [e for e in page.events if e[0] == "mouse_move"]
    assert len(moves) >= 2
    # The click lands at the cursor's realized point (mouse.click), and the hover
    # relies on the approach move — neither re-snaps via a native locator call.
    assert any(e[0] == "mouse_click" for e in page.events)
    assert ("locator_click", None) not in page.events
    assert ("hover", None) not in page.events
