"""First-run verdict + automated scheduling (workstream D).

Contract used by the ``/api/imposter5/run`` pipeline AFTER a run completes:

    outcome = finalize_run(provider=..., url=..., prompt=..., result=..., schedule=...)

``outcome`` is a :class:`RunOutcome` (public contract, kept stable):

- ``verdict``        -> "green" when the first run succeeded end-to-end, else "blocked".
- ``scheduled``      -> True when the task was enrolled on a recurring schedule.
- ``interval_minutes`` / ``next_run_at`` -> the cadence, when scheduled.
- ``reason``         -> human-readable explanation (why blocked / why not scheduled).
- ``.to_payload()``  -> JSON-safe dict for the API response.

The intended flow the user described: always do the first run, report green, and
ONLY then arm an automated schedule. So scheduling is gated on a green verdict —
a blocked run is never enrolled, no matter what the caller asked for.

Scheduling enrollment is requested via the optional ``schedule`` argument, e.g.
``finalize_run(..., schedule={"interval_minutes": 60})``. The interval ultimately
comes from the request layer (``AutomationConnectorTargetRequest.check_interval_minutes``);
the integrator threads it into this seam. When ``schedule`` is omitted the run is
finalized with a verdict only (current pipeline behavior), which keeps the seam
backward compatible until the endpoint passes a schedule.

Worker entrypoints (wire up from a poller / process supervisor — NOT auto-started
here): :func:`run_due_tasks` for one-shot polling and :func:`start_worker` /
:func:`stop_worker` for a background thread loop. Both re-launch due tasks through
a pluggable run callable (see :func:`set_run_callable`) so this module never has
to import the FastAPI endpoint.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from imposter5.automation_connector.task_store import (
    TaskRecord,
    TaskStore,
    utcnow,
)

logger = logging.getLogger(__name__)

# Default cadence floor mirrors AutomationConnectorTargetRequest.check_interval_minutes (ge=5).
MIN_INTERVAL_MINUTES = 5

# A run callable: (provider, url, prompt) -> result dict shaped like the one the
# endpoint builds ({"success": bool, "goal": ..., "session_recording": ...}).
RunCallable = Callable[[str, str, str | None], dict[str, Any]]


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


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
def _has_real_goal(goal: Any) -> bool:
    """A goal counts as real when it carries a name or at least one step."""
    if not isinstance(goal, dict):
        return bool(goal)
    if goal.get("steps"):
        return True
    return bool(goal.get("name"))


def derive_verdict(result: dict[str, Any] | None) -> tuple[str, str]:
    """Map a completed run's result into ``(verdict, reason)``.

    Green requires the run to report success AND to have produced real evidence —
    a compiled goal or a session recording. A success with nothing to show for it
    is reported as blocked rather than pretending it accomplished the task.
    """
    result = result or {}
    if not bool(result.get("success", False)):
        reason = str(result.get("error") or result.get("reason") or "run did not succeed")
        return "blocked", reason

    has_goal = _has_real_goal(result.get("goal"))
    has_recording = bool(result.get("session_recording"))
    if not (has_goal or has_recording):
        return "blocked", "run reported success but produced no goal or session recording"
    return "green", ""


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
def _resolve_interval(schedule: dict[str, Any] | None) -> int | None:
    """Pull a usable interval (minutes) out of an enrollment request, or None."""
    if not schedule:
        return None
    raw = schedule.get("interval_minutes")
    if raw is None:
        raw = schedule.get("check_interval_minutes")
    if raw is None:
        return None
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return None
    if minutes < MIN_INTERVAL_MINUTES:
        minutes = MIN_INTERVAL_MINUTES
    return minutes


def finalize_run(
    *,
    provider: str,
    url: str,
    prompt: str | None = None,
    result: dict[str, Any] | None = None,
    schedule: dict[str, Any] | None = None,
    store: TaskStore | None = None,
    now: datetime | None = None,
) -> RunOutcome:
    """Turn a completed first run into a verdict and (optionally) enroll a schedule.

    ``store`` / ``now`` are injection points for tests; production callers pass
    neither and get the default durable store and wall clock.
    """
    verdict, reason = derive_verdict(result)

    interval = _resolve_interval(schedule)
    if interval is None:
        # No schedule requested: verdict-only finalize.
        return RunOutcome(verdict=verdict, scheduled=False, reason=reason)

    if verdict != "green":
        # Scheduling is gated on a green first run.
        not_scheduled_reason = reason or "first run was not green; not scheduling"
        return RunOutcome(verdict=verdict, scheduled=False, reason=not_scheduled_reason)

    moment = now or utcnow()
    active_store = store or TaskStore()
    record = active_store.enroll(
        provider=provider,
        url=url,
        prompt=prompt,
        interval_minutes=interval,
        last_verdict=verdict,
        now=moment,
    )
    return RunOutcome(
        verdict="green",
        scheduled=True,
        interval_minutes=record.interval_minutes,
        next_run_at=record.next_run_at,
        reason="",
    )


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
_run_callable: RunCallable | None = None


def set_run_callable(fn: RunCallable | None) -> None:
    """Register the thin internal callable the worker uses to re-launch a run.

    The integrator wires this to in-process run logic (NOT the FastAPI endpoint),
    e.g. ``set_run_callable(lambda provider, url, prompt: launch_run(...))``.
    Tests inject a fake here. Left unset, the worker skips execution and reports
    a blocked verdict for each due task so nothing silently "succeeds".
    """
    global _run_callable
    _run_callable = fn


def _default_run(provider: str, url: str, prompt: str | None) -> dict[str, Any]:
    """Fallback when no run callable is registered: do not fabricate success."""
    logger.warning(
        "imposter5 scheduler: no run callable registered; skipping due task %s %s",
        provider,
        url,
    )
    return {"success": False, "reason": "no run callable registered"}


def run_due_tasks(
    *,
    store: TaskStore | None = None,
    runner: RunCallable | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Execute every currently-due task once and reschedule it.

    Returns one summary dict per task it attempted: ``{task_id, provider, url,
    verdict}``. Safe to call repeatedly from a poller; a task that is not yet due
    is left untouched.
    """
    active_store = store or TaskStore()
    run = runner or _run_callable or _default_run
    moment = now or utcnow()

    summaries: list[dict[str, Any]] = []
    for task in active_store.due_tasks(now=moment):
        verdict = "blocked"
        try:
            result = run(task.provider, task.url, task.prompt)
            verdict, _reason = derive_verdict(result)
        except Exception as exc:  # a failing run must not kill the worker loop
            logger.exception("imposter5 scheduler: due task %s raised: %s", task.id, exc)
            verdict = "blocked"
        active_store.mark_ran(task.id, verdict=verdict, now=now or utcnow())
        summaries.append(
            {
                "task_id": task.id,
                "provider": task.provider,
                "url": task.url,
                "verdict": verdict,
            }
        )
    return summaries


@dataclass
class WorkerHandle:
    """Control surface for a background worker thread."""

    thread: threading.Thread
    stop_event: threading.Event

    def stop(self, *, timeout: float | None = 5.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=timeout)


def start_worker(
    *,
    poll_seconds: float = 60.0,
    store: TaskStore | None = None,
    runner: RunCallable | None = None,
) -> WorkerHandle:
    """Start a daemon thread that polls for due tasks every ``poll_seconds``.

    NOT auto-started by the app — the integrator calls this from a supervised
    entrypoint. Returns a :class:`WorkerHandle`; call ``handle.stop()`` (or
    :func:`stop_worker`) to shut it down.
    """
    active_store = store or TaskStore()
    stop_event = threading.Event()

    def _loop() -> None:
        while not stop_event.is_set():
            try:
                run_due_tasks(store=active_store, runner=runner)
            except Exception as exc:  # never let the loop die on a single error
                logger.exception("imposter5 scheduler worker pass failed: %s", exc)
            stop_event.wait(poll_seconds)

    thread = threading.Thread(target=_loop, name="imposter5-scheduler", daemon=True)
    thread.start()
    return WorkerHandle(thread=thread, stop_event=stop_event)


def stop_worker(handle: WorkerHandle, *, timeout: float | None = 5.0) -> None:
    """Signal a worker started by :func:`start_worker` to stop and join it."""
    handle.stop(timeout=timeout)
