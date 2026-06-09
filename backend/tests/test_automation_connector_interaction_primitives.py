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

    def click(self, x: float, y: float) -> None:
        # Real Playwright Mouse.click(x, y) presses at the cursor's current
        # (realized) point; the humanized click uses this instead of
        # locator.click() so Playwright never recenters to the element middle.
        self.events.append(("mouse_click", {"x": round(x), "y": round(y)}))


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

    def click(self) -> None:
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

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.mouse = FakeMouse(self.events)

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
    plan = {"run_id": "r1", "pacing": {"wait_ms": [321], "scroll_delta_y": [654]}}

    assert wait_human(page, plan, 0, 800) == 321
    assert scroll_page(page, plan, 0, 900) == 654

    # Positioning for realistic mouse+wheel (the quality improvement) now probes
    # for the content area via a fast-fail query_selector (NOT locator.bounding_box,
    # which auto-waits ~20s per missing selector) + mouse_move (via move_pointer)
    # before the wheel. We assert the planned values are honored and that the
    # human-like mouse events for scroll happen.
    evs = page.events
    assert ("wait", 321) in evs
    # Scroll now models momentum bleed-off: a decaying burst of wheel events whose
    # signed deltas sum to (approximately) the planned 654px, not one raw wheel.
    wheels = [e[1]["delta_y"] for e in evs if e[0] == "wheel"]
    assert len(wheels) >= 2, "scroll should emit a multi-step decaying wheel burst"
    assert all(d > 0 for d in wheels), "downward scroll => all-positive wheel deltas"
    assert abs(sum(wheels) - 654) <= 2, "decaying burst should sum to the planned delta"
    assert any(e[0] == "query_selector" for e in evs), "scroll should fast-fail probe for content area to position mouse"
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
