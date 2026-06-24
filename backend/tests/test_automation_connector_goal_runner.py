from __future__ import annotations

from typing import Any

from imposter5.automation_connector.goal_runner import compile_goal_actions, run_visible_state_goal
from imposter5.automation_connector.goals import goal_spec_from_target
from imposter5.automation_connector.session_recorder import SessionRecorder


class FakeMouse:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events

    def wheel(self, delta_x: int, delta_y: int) -> None:
        self.events.append(("wheel", {"delta_x": delta_x, "delta_y": delta_y}))

    def move(self, x: float, y: float) -> None:
        # Humanized scroll positions the cursor over content before the wheel burst.
        self.events.append(("mouse_move", {"x": round(x), "y": round(y)}))


class FakePage:
    # Humanized aimed movement reads viewport_size to seed the initial cursor.
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.mouse = FakeMouse(self.events)

    def goto(self, url: str, *, wait_until: str) -> None:
        self.events.append(("goto", {"url": url, "wait_until": wait_until}))

    def wait_for_timeout(self, wait_ms: int) -> None:
        self.events.append(("wait", wait_ms))

    def title(self) -> str:
        return "Accounts"

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return "Three customers renewed this week."


def test_compile_goal_actions_turns_prompt_goal_into_bounded_steps() -> None:
    goal = goal_spec_from_target(
        {
            "entity_id": "https://example.com/accounts",
            "payload_prompt": "Return renewal signals as JSON.",
        }
    )
    plan = {"completion": {"max_scroll_passes": 2}}

    actions = compile_goal_actions(goal, plan)

    assert [action["type"] for action in actions] == [
        "goto",
        "wait",
        "inspect_visible_state",
        "scroll",
        "wait",
        "record_visible_state",
    ]


def test_run_visible_state_goal_records_actions_and_visible_state() -> None:
    page = FakePage()
    goal = goal_spec_from_target({"entity_id": "https://example.com/accounts"})
    plan = {
        "run_id": "run-2",
        # Pin the session seed so the per-session RNG is deterministic (an absent seed
        # falls back to os.urandom, which made this assertion flaky), and disable the
        # stochastic scroll overshoot so this test pins the PLANNED decaying burst
        # rather than the occasional reverse correction (covered in the motor tests).
        "session_seed": "goal-2",
        "human_config": {"scroll_overshoot_chance": 0.0},
        "completion": {"max_scroll_passes": 2},
        "pacing": {"wait_ms": [111, 222], "scroll_delta_y": [333]},
        "recorder": {"enabled": True, "max_events": 20},
        "analytics": {"synthetic": True, "labels": ["automation_connector"]},
    }
    recorder = SessionRecorder(plan)

    result = run_visible_state_goal(page, goal, plan, recorder=recorder)

    assert result["title"] == "Accounts"
    assert result["summary"] == "Three customers renewed this week."
    # Scroll is now a decaying wheel burst (momentum bleed-off) summing to the
    # planned 333px, rather than one raw wheel event.
    wheels = [e[1]["delta_y"] for e in page.events if e[0] == "wheel"]
    assert len(wheels) >= 2
    assert all(d > 0 for d in wheels)
    assert abs(sum(wheels) - 333) <= 2
    assert result["session_recording"]["event_count"] >= 5
