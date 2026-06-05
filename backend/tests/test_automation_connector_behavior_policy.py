from __future__ import annotations

import json

from imposter5.automation_connector.behavior_policy import (
    behavior_summary,
    build_behavior_plan,
    planned_scroll_passes,
)
from imposter5.automation_connector.goals import goal_spec_from_target


def test_behavior_plan_contains_goal_and_analytics_labels() -> None:
    target = {
        "id": "target-1",
        "entity_id": "https://example.com/accounts",
        "payload_prompt": "Return renewal signals as JSON.",
    }
    goal = goal_spec_from_target(target)

    plan = build_behavior_plan(target, provider="generic_web", goal=goal, seed="stable")
    summary = behavior_summary(plan)

    assert plan["goal_spec"]["start_url"] == "https://example.com/accounts"
    assert plan["goal_spec"]["prompt"] == "Return renewal signals as JSON."
    assert summary["goal"] == "observe_visible_page_state"
    assert "automation_connector" in summary["analytics_labels"]
    assert "provider:generic_web" in summary["analytics_labels"]


def test_completion_ladder_is_configurable_from_target() -> None:
    target = {
        "id": "target-2",
        "entity_id": "https://example.com",
        "completion_ladder": [
            {"name": "visible_only", "weight": 100, "max_scroll_passes": 1},
        ],
    }

    plan = build_behavior_plan(target, provider="generic_web", seed="stable")

    assert plan["completion"]["name"] == "visible_only"
    assert planned_scroll_passes(plan, 4) == 1


def test_completion_ladder_is_configurable_from_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "AUTOMATION_CONNECTOR_COMPLETION_LADDER_JSON",
        json.dumps([{"name": "deep_but_bounded", "weight": 100, "max_scroll_passes": 3}]),
    )

    plan = build_behavior_plan({"id": "target-3"}, provider="generic_web", seed="stable")

    assert plan["completion"]["name"] == "deep_but_bounded"
    assert planned_scroll_passes(plan, 4) == 3


def test_typing_and_pointer_knobs_are_bounded() -> None:
    target = {
        "id": "target-4",
        "typing_typo_chance": 1,
        "typing_max_delay_ms": 10_000,
        "hover_before_click_chance": 1,
        "pointer_imprecision_px": 99,
    }

    plan = build_behavior_plan(target, provider="generic_web", seed="stable")

    assert plan["typing"]["typo_chance"] == 0.08
    assert plan["typing"]["max_delay_ms"] == 800
    assert plan["pointer"]["hover_before_click_chance"] == 0.6
    assert plan["pointer"]["imprecision_px"] == 10


def test_lower_half_behavior_knobs_are_bounded_and_labeled() -> None:
    target = {
        "id": "target-5",
        "automation_device": "mobile",
        "expand_comments_chance": 1,
        "max_comment_expansions": 99,
        "micro_abandon_chance": 1,
        "max_backtracks": 99,
        "session_recorder_max_events": 9_999,
    }

    plan = build_behavior_plan(target, provider="generic_web", seed="stable")
    summary = behavior_summary(plan)

    assert plan["hover"]["expand_comments_chance"] == 0.35
    assert plan["hover"]["max_expansions"] == 3
    assert plan["backtracking"]["micro_abandon_chance"] == 0.25
    assert plan["backtracking"]["max_backtracks"] == 2
    assert plan["mobile"]["enabled"] is True
    assert plan["recorder"]["max_events"] == 500
    assert "device:mobile" in plan["analytics"]["labels"]
    assert summary["device"] == "mobile"


def test_empty_behavior_summary_stays_empty() -> None:
    assert behavior_summary({}) == {}
    assert behavior_summary(None) == {}
