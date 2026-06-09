"""First-run verdict + automated scheduling (pipeline seam — workstream D fills in).

Contract used by the ``/api/imposter5/run`` pipeline AFTER a run completes:

    outcome = finalize_run(provider=..., url=..., prompt=..., result=..., schedule=...)

``outcome`` is a :class:`RunOutcome`:

- ``verdict``        -> "green" when the first run succeeded end-to-end, else "blocked".
- ``scheduled``      -> True when the task was enrolled on a recurring schedule.
- ``interval_minutes`` / ``next_run_at`` -> the cadence, when scheduled.

The intended flow the user described: always do the first run, report green, and
only then arm an automated schedule. This stub reports the verdict from whatever
the run returned and never schedules, so behavior is unchanged until workstream D
implements: a durable task store, ``check_interval_minutes`` enrollment, and a
worker that executes due tasks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RunOutcome:
    """Verdict for a completed first run and its scheduling state."""

    verdict: str = "green"  # "green" | "blocked"
    scheduled: bool = False
    interval_minutes: int | None = None
    next_run_at: str | None = None
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "scheduled": self.scheduled,
            "interval_minutes": self.interval_minutes,
            "next_run_at": self.next_run_at,
            "reason": self.reason,
        }


def finalize_run(
    *,
    provider: str,
    url: str,
    prompt: str | None = None,
    result: dict[str, Any] | None = None,
    schedule: dict[str, Any] | None = None,
) -> RunOutcome:
    """Turn a completed first run into a verdict and (optionally) enroll a schedule.

    STUB: reports ``green`` and never schedules. Workstream D replaces the body
    with a real success determination + durable enrollment on
    ``check_interval_minutes`` plus a worker that runs due tasks.
    """
    result = result or {}
    ran_ok = bool(result.get("success", True))
    return RunOutcome(
        verdict="green" if ran_ok else "blocked",
        scheduled=False,
        interval_minutes=None,
        next_run_at=None,
        reason="scheduling not yet implemented",
    )
