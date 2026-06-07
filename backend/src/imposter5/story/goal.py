"""Goal checker: decide when a Story session is "done enough".

Humans don't terminate a task on an exact threshold — they over- or undershoot. So
the checker draws an EFFECTIVE target once per session by perturbing the predicate's
target by its jitter (seeded from the session RNG for reproducibility), then reports
satisfaction against live execution state. The executor owns the state; this module
is pure decision logic so it is trivially unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from imposter5.story.task_intent import GoalPredicate


@dataclass
class GoalState:
    """Mutable execution counters the checker reads (objective progress only).

    Tangent/off-goal activity deliberately does NOT advance these counters — wander
    is off-goal by construction.
    """

    results_total: int = 0
    results_scanned: int = 0
    profiles_opened: int = 0
    profiles_read: int = 0

    @property
    def scan_fraction(self) -> float:
        if self.results_total <= 0:
            return 0.0
        return min(1.0, self.results_scanned / float(self.results_total))


class GoalChecker:
    """Evaluate a :class:`GoalPredicate` against a :class:`GoalState`."""

    def __init__(self, predicate: GoalPredicate, rng: Any) -> None:
        self.predicate = predicate
        self.effective_target = self._draw_effective_target(predicate, rng)

    @staticmethod
    def _draw_effective_target(predicate: GoalPredicate, rng: Any) -> float:
        """Perturb the nominal target by +/- jitter (human over/undershoot)."""
        target = float(predicate.target)
        jitter = float(predicate.jitter)
        if jitter <= 0.0:
            return target
        if predicate.type == "scan_fraction":
            # Jitter is an absolute fraction; clamp to a sane scanning band.
            eff = target + rng.uniform(-jitter, jitter)
            return max(0.05, min(1.0, eff))
        # Count-style targets: jitter is a fraction of the target count.
        eff = target * (1.0 + rng.uniform(-jitter, jitter))
        return max(1.0, eff)

    def is_satisfied(self, state: GoalState) -> bool:
        ptype = self.predicate.type
        if ptype == "scan_fraction":
            return state.scan_fraction >= self.effective_target
        if ptype == "open_count":
            return state.profiles_opened >= self.effective_target
        if ptype == "find_in_profile":
            return state.profiles_read >= self.effective_target
        return False

    def to_payload(self, state: GoalState) -> dict[str, Any]:
        return {
            "type": self.predicate.type,
            "nominal_target": self.predicate.target,
            "jitter": self.predicate.jitter,
            "effective_target": round(self.effective_target, 4),
            "satisfied": self.is_satisfied(state),
            "state": {
                "results_total": state.results_total,
                "results_scanned": state.results_scanned,
                "scan_fraction": round(state.scan_fraction, 4),
                "profiles_opened": state.profiles_opened,
                "profiles_read": state.profiles_read,
            },
        }
