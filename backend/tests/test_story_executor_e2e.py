from __future__ import annotations

from imposter5.automation_connector.behavior_policy import build_behavior_plan
from imposter5.story.executor import run_story
from imposter5.story.task_intent import parse_task_intent

INTENT_RAW = {
    "schema": "lhhl-task-intent/v1",
    "site": "gauntlet",
    "archetype": "social_feed",
    "describe": "search data engineers, scan results, open profiles, read",
    "objective": {
        "main_scenes": ["search_open", "search_query", "results_scan", "profile_open", "profile_read", "profile_back"],
        "query_hint": "data engineers",
        "goal_predicate": {"type": "scan_fraction", "target": 0.5, "jitter": 0.15},
    },
    # High tangent budget so a curiosity excursion reliably fires in the e2e run.
    "curiosity": {
        "tangent_chance": 0.95,
        "max_tangents": 3,
        "max_depth": 1,
        "tangent_scenes": ["tangent_open_profile", "tangent_read", "tangent_back", "tangent_research", "tangent_refresh"],
    },
    "variance": {"dwell_cv": 0.4, "order_jitter": True, "partial_substitution": True},
}


def _controlled_plan(seed: str):
    # Force a non-naive persona so motion is the continuous analog path (never the
    # naive_bot teleport), and pin the session seed for reproducibility.
    plan = build_behavior_plan(
        {"id": "story-test", "entity_type": "generic_web"},
        provider="generic", goal="story_task_intent", seed=seed,
    )
    plan["persona"] = {"name": "focused_power_user", "patience": "medium",
                       "scroll_style": "direct_scan", "interaction_style": "low_touch"}
    plan.setdefault("recorder", {})["enabled"] = True
    plan["recorder"]["max_events"] = 500
    return plan


def test_story_end_to_end_against_fixture(gauntlet_page) -> None:
    intent = parse_task_intent(INTENT_RAW)
    seed = "e2e-7"
    bplan = _controlled_plan(seed)
    result = run_story(
        gauntlet_page, intent, seed=seed, behavior_plan=bplan,
        dwell_scale=0.0, ambient_steps=1, max_scan_passes=10, speed_scale=0.0,
    )

    # 1. Goal predicate satisfied (scanned >= jittered target).
    assert result["goal_met"] is True
    goal = result["goal"]
    assert goal["state"]["scan_fraction"] >= goal["effective_target"] - 1e-9
    assert goal["state"]["results_total"] == 24  # honeypots excluded from the set

    # 2. A search was typed and submitted.
    typed = [e for e in result["trace"] if e["scene"] == "search_query" and e.get("status") == "ok"]
    assert typed, "search query should have been typed + submitted"

    # 3. At least one profile opened (objective and/or curiosity tangent).
    assert result["profiles_opened_total"] >= 1

    # 4. At least one curiosity tangent fired AND returned; resume stack balanced.
    assert result["tangents_fired"] >= 1
    assert result["tangents_returned"] == result["tangents_fired"]
    assert result["resume_stack_balanced"] is True

    # 5. Motion is continuous analog (no teleport): recorded mouse moves are the
    #    multi-step ballistic primitive, never the naive 'direct' jump.
    events = result["recorder"]["events"]
    moves = [e for e in events if e["action"] == "mouse_move"]
    assert moves, "expected recorded analog mouse movement"
    assert all(e["metadata"].get("style") != "direct" for e in moves), "no teleporting moves allowed"
    assert any(int(e["metadata"].get("steps", 0)) > 1 for e in moves), "moves should be multi-step curves"

    # 6. Honeypots untouched: no trap element was ever opened.
    opened = [e.get("element", "") for e in result["trace"] if "element" in e]
    assert not any("trap" in k for k in opened), f"a honeypot was opened: {opened}"


def test_two_runs_differ_but_both_meet_goal(gauntlet_page_factory) -> None:
    intent = parse_task_intent(INTENT_RAW)
    # Each attempt runs on its OWN fresh page (clean browser state), matching how
    # production runs one session per page.
    results = []
    for seed in ("alpha", "bravo"):
        page = gauntlet_page_factory()
        bplan = _controlled_plan(seed)
        results.append(run_story(page, intent, seed=seed, behavior_plan=bplan,
                                 dwell_scale=0.0, ambient_steps=0, max_scan_passes=8, speed_scale=0.0))
    a, b = results
    assert a["goal_met"] and b["goal_met"]
    # No two attempts alike: the executed scene sequences differ.
    seq_a = [(e["scene"], e["kind"]) for e in a["trace"]]
    seq_b = [(e["scene"], e["kind"]) for e in b["trace"]]
    assert seq_a != seq_b, "two attempts from one prompt should not be identical"
