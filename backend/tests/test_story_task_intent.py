from __future__ import annotations

import json

import pytest

from imposter5.story.task_intent import (
    TaskIntentError,
    load_task_intent,
    parse_task_intent,
)

VALID = {
    "schema": "lhhl-task-intent/v1",
    "site": "gauntlet",
    "archetype": "social_feed",
    "describe": "search data engineers, scan results, open profiles, read",
    "objective": {
        "main_scenes": ["search_open", "search_query", "results_scan", "profile_open", "profile_read", "profile_back"],
        "query_hint": "data engineers",
        "goal_predicate": {"type": "scan_fraction", "target": 0.5, "jitter": 0.15},
    },
    "curiosity": {
        "tangent_chance": 0.25,
        "max_tangents": 3,
        "max_depth": 1,
        "tangent_scenes": ["tangent_open_profile", "tangent_read", "tangent_back", "tangent_research", "tangent_refresh"],
    },
    "variance": {"dwell_cv": 0.4, "order_jitter": True, "partial_substitution": True},
    "cadence": "~1/day",
}


def test_parse_valid_intent_roundtrips_fields() -> None:
    intent = parse_task_intent(VALID)
    assert intent.site == "gauntlet"
    assert intent.query_hint == "data engineers"
    assert intent.goal_predicate.type == "scan_fraction"
    assert intent.goal_predicate.target == 0.5
    assert intent.curiosity.enabled is True
    assert "tangent_open_profile" in intent.curiosity.tangent_scenes
    assert intent.variance.order_jitter is True


def test_defaults_when_optional_blocks_missing() -> None:
    minimal = {
        "site": "x",
        "archetype": "y",
        "describe": "z",
        "objective": {
            "main_scenes": ["search_open", "search_query", "results_scan"],
            "query_hint": "q",
            "goal_predicate": {"type": "scan_fraction", "target": 0.4},
        },
    }
    intent = parse_task_intent(minimal)
    assert intent.curiosity.enabled is False  # no tangent budget by default
    assert intent.variance.dwell_cv == pytest.approx(0.35)
    assert intent.goal_predicate.jitter == 0.0


def test_bad_schema_rejected() -> None:
    bad = dict(VALID, schema="something-else/v9")
    with pytest.raises(TaskIntentError):
        parse_task_intent(bad)


def test_unknown_main_scene_rejected() -> None:
    bad = json.loads(json.dumps(VALID))
    bad["objective"]["main_scenes"] = ["search_open", "frobnicate"]
    with pytest.raises(TaskIntentError):
        parse_task_intent(bad)


def test_unknown_tangent_scene_rejected() -> None:
    bad = json.loads(json.dumps(VALID))
    bad["curiosity"]["tangent_scenes"] = ["tangent_open_profile", "do_a_barrel_roll"]
    with pytest.raises(TaskIntentError):
        parse_task_intent(bad)


def test_scan_fraction_target_out_of_range_rejected() -> None:
    bad = json.loads(json.dumps(VALID))
    bad["objective"]["goal_predicate"]["target"] = 1.5
    with pytest.raises(TaskIntentError):
        parse_task_intent(bad)


def test_bad_predicate_type_rejected() -> None:
    bad = json.loads(json.dumps(VALID))
    bad["objective"]["goal_predicate"]["type"] = "vibes"
    with pytest.raises(TaskIntentError):
        parse_task_intent(bad)


def test_load_from_inline_json_string() -> None:
    intent = load_task_intent(json.dumps(VALID))
    assert intent.archetype == "social_feed"


def test_load_from_file(tmp_path) -> None:
    p = tmp_path / "intent.json"
    p.write_text(json.dumps(VALID), encoding="utf-8")
    intent = load_task_intent(str(p))
    assert intent.query_hint == "data engineers"
