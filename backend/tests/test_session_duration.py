"""Tests for the human session-DURATION model (idea #1).

Guards the property that motivated the work: session *lengths* must form a
heavy-tailed, multi-modal distribution (many sub-minute dips, a few-minute
typical browse, a long tail) — not one fixed ~4-minute budget that spikes the
duration histogram. Mirrors the arrival-clock test philosophy: assert the same
shape statistics a detector would compute, with comfortable margins.
"""
from __future__ import annotations

import random
import statistics

import pytest

from imposter5.automation_connector import session_duration as sd
from imposter5.automation_connector.behavior_policy import build_behavior_plan
from imposter5.story.session_planner import _ARCS_BY_NAME, build_feed_intent


def _sample(n: int = 600, seed: str = "dur-seed") -> list[float]:
    return sd.generate_durations(n, seed=seed)


def test_durations_respect_contract_window():
    for d in _sample():
        assert sd.MIN_SECONDS <= d <= sd.MAX_SECONDS


def test_duration_distribution_is_heavy_tailed_multimodal():
    xs = _sample()
    mean = statistics.fmean(xs)
    cv = statistics.pstdev(xs) / mean
    micro_frac = sum(1 for d in xs if d < 60.0) / len(xs)
    long_frac = sum(1 for d in xs if d > 400.0) / len(xs)
    # Heavy tail -> CV well above a fixed (CV=0) or mildly-jittered schedule.
    assert cv > 0.6, cv
    # A real share of "dip in, check one thing, leave" micro-sessions...
    assert micro_frac > 0.15, micro_frac
    # ...and a genuine long tail, so it is not just "short vs medium".
    assert long_frac > 0.06, long_frac


def test_duration_is_deterministic_per_seed():
    assert _sample(seed="x") == _sample(seed="x")
    assert _sample(seed="x") != _sample(seed="y")


def test_persona_scale_shifts_but_does_not_flatten():
    slow = sd.generate_durations(400, seed="s", scale=1.6)
    fast = sd.generate_durations(400, seed="s", scale=0.6)
    assert statistics.median(slow) > statistics.median(fast)
    # Even the stretched draws keep a heavy tail (not collapsed to one length).
    assert statistics.pstdev(slow) / statistics.fmean(slow) > 0.5


def test_micro_dip_arc_exists_and_is_short():
    assert "micro_dip" in _ARCS_BY_NAME
    rng = random.Random(7)
    # A long time budget: the arc, not the budget, should keep the dip tiny.
    dip, _ = build_feed_intent({}, duration_s=600.0, rng=rng, arc_name="micro_dip")
    research, _ = build_feed_intent({}, duration_s=600.0, rng=rng, arc_name="research")
    assert dip.goal_predicate.target < research.goal_predicate.target
    assert dip.goal_predicate.target <= 5


def test_build_behavior_plan_samples_human_duration():
    plan = build_behavior_plan({"id": "t-1"}, provider="generic_web", seed="stable")
    assert sd.MIN_SECONDS <= plan["gauntlet_duration_s"] <= sd.MAX_SECONDS


def test_explicit_duration_override_is_honored():
    plan = build_behavior_plan(
        {"id": "t-2", "gauntlet_duration_s": 45}, provider="generic_web", seed="stable"
    )
    assert plan["gauntlet_duration_s"] == 45.0


def test_duration_uses_independent_rng_stream():
    """The duration draw must not perturb the rest of the seeded plan.

    A pinned ``identity_id`` makes the identity-keyed kinematics deterministic, so
    the only intended difference between the two plans is the session duration.
    """
    base = {"id": "t-3", "identity_id": "person-3"}
    pinned = build_behavior_plan(
        {**base, "gauntlet_duration_s": 120}, provider="generic_web", seed="stable"
    )
    sampled = build_behavior_plan(base, provider="generic_web", seed="stable")
    # Sampling duration from its own substream leaves every other draw identical.
    assert pinned["pointer"] == sampled["pointer"]
    assert pinned["typing"] == sampled["typing"]
    assert pinned["pacing"] == sampled["pacing"]
    assert pinned["gauntlet_duration_s"] == 120.0
    assert sampled["gauntlet_duration_s"] != 120.0
