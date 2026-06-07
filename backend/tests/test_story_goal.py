from __future__ import annotations

import random

from imposter5.story.goal import GoalChecker, GoalState
from imposter5.story.task_intent import GoalPredicate


def test_scan_fraction_effective_target_within_jitter_band() -> None:
    pred = GoalPredicate(type="scan_fraction", target=0.5, jitter=0.15)
    for s in range(50):
        gc = GoalChecker(pred, random.Random(s))
        assert 0.35 - 1e-9 <= gc.effective_target <= 0.65 + 1e-9


def test_scan_fraction_satisfied() -> None:
    pred = GoalPredicate(type="scan_fraction", target=0.5, jitter=0.0)
    gc = GoalChecker(pred, random.Random(0))
    st = GoalState(results_total=20, results_scanned=9)
    assert not gc.is_satisfied(st)
    st.results_scanned = 10
    assert gc.is_satisfied(st)


def test_open_count_predicate() -> None:
    pred = GoalPredicate(type="open_count", target=3, jitter=0.0)
    gc = GoalChecker(pred, random.Random(0))
    st = GoalState(profiles_opened=2)
    assert not gc.is_satisfied(st)
    st.profiles_opened = 3
    assert gc.is_satisfied(st)


def test_find_in_profile_predicate() -> None:
    pred = GoalPredicate(type="find_in_profile", target=1, jitter=0.0)
    gc = GoalChecker(pred, random.Random(0))
    st = GoalState()
    assert not gc.is_satisfied(st)
    st.profiles_read = 1
    assert gc.is_satisfied(st)


def test_no_jitter_uses_exact_target() -> None:
    pred = GoalPredicate(type="scan_fraction", target=0.5, jitter=0.0)
    gc = GoalChecker(pred, random.Random(123))
    assert gc.effective_target == 0.5
