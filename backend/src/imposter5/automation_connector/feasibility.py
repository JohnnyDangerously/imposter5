"""Pre-execution feasibility / action review (pipeline seam — workstream C fills in).

Contract used by the ``/api/imposter5/run`` pipeline, called with the browser
already open on the target page (so the DOM can be inspected) but BEFORE the
compiled goal is executed:

    report = review_feasibility(page, goal, plan)

``report`` is a :class:`FeasibilityReport`. The pipeline reads it:

- ``status == "ok"``        -> every required step has a resolvable target; run.
- ``status == "skipped"``   -> review not performed; run (current stub behavior).
- ``status == "infeasible"``-> at least one REQUIRED step cannot be performed on
  this page (e.g. user asked to click "Messages" but no such affordance exists);
  SHORT-CIRCUIT and return ``to_payload()`` so the UI can tell the user which
  steps are not possible and why, instead of silently clicking a fallback spot.

This stub returns ``skipped`` so nothing is blocked until workstream C implements
the real dry-run using ``story.site_mapper.SiteMapper`` to resolve each compiled
``GoalStep``'s target against the live affordance map.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepFeasibility:
    """Per-step verdict from the action review."""

    step: str
    action: str
    feasible: bool
    required: bool = True
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action": self.action,
            "feasible": self.feasible,
            "required": self.required,
            "reason": self.reason,
        }


@dataclass
class FeasibilityReport:
    """Outcome of reviewing a compiled goal against the live page."""

    status: str = "skipped"  # "ok" | "infeasible" | "skipped"
    steps: list[StepFeasibility] = field(default_factory=list)
    summary: str = ""

    @property
    def blocks_run(self) -> bool:
        return self.status == "infeasible"

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "steps": [s.to_payload() for s in self.steps],
            "blocks_run": self.blocks_run,
        }


def review_feasibility(page: Any, goal: Any, plan: dict[str, Any] | None = None) -> FeasibilityReport:
    """Dry-run a compiled goal against the live DOM and report can/can't per step.

    STUB: currently returns ``skipped`` (does not inspect the page or block).
    Workstream C replaces the body with a SiteMapper-backed resolution of every
    required step's target, marking infeasible steps with a human-readable reason.
    """
    return FeasibilityReport(status="skipped", steps=[], summary="feasibility review not yet implemented")
