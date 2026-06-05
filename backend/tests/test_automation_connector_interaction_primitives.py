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


class FakeLocator:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events

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


class FakePage:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.mouse = FakeMouse(self.events)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.events.append(("wait", timeout_ms))

    def locator(self, selector: str) -> FakeLocator:
        self.events.append(("locator", selector))
        return FakeLocator(self.events)

    def go_back(self, *, wait_until: str) -> None:
        self.events.append(("go_back", wait_until))


def test_wait_and_scroll_use_planned_values() -> None:
    page = FakePage()
    plan = {"run_id": "r1", "pacing": {"wait_ms": [321], "scroll_delta_y": [654]}}

    assert wait_human(page, plan, 0, 800) == 321
    assert scroll_page(page, plan, 0, 900) == 654

    # Positioning for realistic mouse+wheel (the quality improvement) now emits locator (content probe)
    # + mouse_move (via move_pointer or safe) before the wheel. We assert the planned values are honored
    # and that the human-like mouse events for scroll happen (instead of exact old sequence, which
    # would have regressed the "mouse scroll event" feature).
    evs = page.events
    assert ("wait", 321) in evs
    assert ("wheel", {"delta_x": 0, "delta_y": 654}) in evs
    assert any(e[0] == "locator" for e in evs), "scroll should probe for content area to position mouse"
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

    assert result == {"hovered": True, "move_style": "two_step"}
    assert ("hover", None) in page.events
    assert ("locator_click", None) in page.events


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
    assert expand_result == {"attempted": 1, "expanded": 1, "max_expansions": 1}
    assert swipe_result == {"swiped": True, "delta_y": 444, "gesture_style": "short_swipe"}
    assert recorder.payload()["event_count"] >= 3


def test_backtrack_is_bounded_and_optional() -> None:
    page = FakePage()
    plan = {
        "run_id": "r5",
        "backtracking": {"micro_abandon_chance": 0, "max_backtracks": 1},
    }

    result = maybe_backtrack(page, plan)

    assert result == {"backtracked": False, "max_backtracks": 1}
    assert ("go_back", "domcontentloaded") not in page.events


def test_move_pointer_executes_planned_styles() -> None:
    page = FakePage()
    plan = {
        "run_id": "rm1",
        "pointer": {"move_style": "two_step", "imprecision_px": 0, "overshoot_chance": 0},
    }

    res = move_pointer(page, 640, 360, plan)
    assert res["style"] == "two_step"
    assert res["overshot"] is False
    assert len([e for e in page.events if e[0] == "mouse_move"]) == 2

    plan2 = {"run_id": "rm2", "pointer": {"move_style": "slight_arc", "imprecision_px": 1, "overshoot_chance": 0}}
    res2 = move_pointer(page, 100, 100, plan2)
    assert res2["style"] == "slight_arc"
    assert len([e for e in page.events if e[0] == "mouse_move"]) > 2

    plan3 = {"run_id": "rm3", "pointer": {"move_style": "direct", "overshoot_chance": 1.0}}
    res3 = move_pointer(page, 50, 50, plan3)
    assert res3["style"] == "direct"
    assert res3["overshot"] is True


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
    assert ("locator_click", None) in page.events
