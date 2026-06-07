from __future__ import annotations

from imposter5.story.compiler import compile_story
from imposter5.story.task_intent import parse_task_intent

BASE = {
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
        "tangent_chance": 0.5,
        "max_tangents": 3,
        "max_depth": 1,
        "tangent_scenes": ["tangent_open_profile", "tangent_read", "tangent_back", "tangent_research", "tangent_refresh"],
    },
    "variance": {"dwell_cv": 0.4, "order_jitter": True, "partial_substitution": True},
}


def _intent(**over):
    raw = {**BASE}
    for k, v in over.items():
        raw[k] = v
    return parse_task_intent(raw)


def _resume_balance_ok(plan) -> bool:
    """Every tangent excursion must return: a depth-aware push/pop balance.

    Each non-return tangent scene that begins navigation pushes; each is_return
    pops. We track per-depth and require the stack to return to empty by the end,
    and never go negative.
    """
    pushes = {"tangent_open_profile", "tangent_research"}  # navigation pushers
    self_return = {"tangent_refresh"}  # push+pop in one
    stack = 0
    for s in plan.scenes:
        if not s.tangent:
            continue
        if s.name in self_return:
            continue  # net-zero excursion
        if s.is_return:
            stack -= 1
            if stack < 0:
                return False
        elif s.name in pushes:
            stack += 1
    return stack == 0


def test_no_two_plans_identical_across_seeds() -> None:
    intent = _intent()
    sigs = {compile_story(intent, seed=str(s)).signature() for s in range(40)}
    # Structural variety: the vast majority of seeds yield distinct scene graphs.
    assert len(sigs) >= 30, f"only {len(sigs)} distinct structural plans of 40"


def test_dwell_varies_even_when_structure_repeats() -> None:
    intent = _intent(curiosity={"tangent_chance": 0.0, "max_tangents": 0, "max_depth": 1, "tangent_scenes": []},
                     variance={"dwell_cv": 0.5, "order_jitter": False, "partial_substitution": False})
    dwell_sets = {tuple(s.dwell_ms for s in compile_story(intent, seed=str(s)).scenes) for s in range(20)}
    assert len(dwell_sets) >= 18, "variable dwell should make plans differ run-to-run"


def test_goal_scenes_always_reachable() -> None:
    # Even when the intent omits required scenes, the compiler injects them.
    intent = _intent(objective={
        "main_scenes": ["results_scan"],
        "query_hint": "q",
        "goal_predicate": {"type": "open_count", "target": 2, "jitter": 0.0},
    })
    for s in range(20):
        names = [sc.name for sc in compile_story(intent, seed=str(s)).main_scenes]
        assert "search_open" in names and "search_query" in names
        assert "results_scan" in names and "profile_open" in names


def test_tangents_bounded_and_always_return() -> None:
    intent = _intent()
    for s in range(60):
        plan = compile_story(intent, seed=str(s))
        assert plan.tangent_count <= intent.curiosity.max_tangents
        assert max((sc.depth for sc in plan.scenes), default=0) <= intent.curiosity.max_depth
        assert _resume_balance_ok(plan), f"seed {s} left a tangent unreturned"


def test_nested_tangents_respect_max_depth_and_return() -> None:
    intent = _intent(curiosity={
        "tangent_chance": 0.95, "max_tangents": 6, "max_depth": 2,
        "tangent_scenes": ["tangent_open_profile", "tangent_read", "tangent_back", "tangent_research"],
    })
    saw_depth_2 = False
    for s in range(60):
        plan = compile_story(intent, seed=str(s))
        assert max((sc.depth for sc in plan.scenes), default=0) <= 2
        assert plan.tangent_count <= 6
        assert _resume_balance_ok(plan)
        if any(sc.depth == 2 for sc in plan.scenes):
            saw_depth_2 = True
    assert saw_depth_2, "high tangent_chance + max_depth=2 should produce nested tangents"


def test_order_jitter_changes_body_order() -> None:
    jittered = _intent()
    orders = {tuple(sc.name for sc in compile_story(jittered, seed=str(s)).main_scenes) for s in range(40)}
    assert len(orders) >= 5, "order jitter + substitution should vary main-scene order/count"


def test_no_curiosity_means_no_tangents() -> None:
    intent = _intent(curiosity={"tangent_chance": 0.0, "max_tangents": 0, "max_depth": 1, "tangent_scenes": []})
    for s in range(15):
        plan = compile_story(intent, seed=str(s))
        assert plan.tangent_count == 0
        assert all(not sc.tangent for sc in plan.scenes)


def test_seed_is_reproducible() -> None:
    intent = _intent()
    a = compile_story(intent, seed="fixed-7")
    b = compile_story(intent, seed="fixed-7")
    assert a.signature() == b.signature()
    assert [s.dwell_ms for s in a.scenes] == [s.dwell_ms for s in b.scenes]
