"""Tests for workstream D: first-run verdict + automated scheduling.

All state is forced into a temp store path and the clock is injected, so these
tests are deterministic and never write to the real durable location.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from imposter5.automation_connector import scheduler
from imposter5.automation_connector.scheduler import (
    RunOutcome,
    derive_verdict,
    finalize_run,
    run_due_tasks,
    set_run_callable,
    start_worker,
)
from imposter5.automation_connector.task_store import (
    TaskStore,
    from_iso,
    task_id_for,
)

FIXED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

GOOD_RESULT = {
    "success": True,
    "goal": {"name": "observe_visible_page_state", "steps": [{"name": "scroll"}]},
    "session_recording": {"events": [{"t": 0}]},
}


@pytest.fixture()
def store(tmp_path):
    return TaskStore(path=tmp_path / "tasks.json")


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
def test_successful_run_is_green():
    verdict, reason = derive_verdict(GOOD_RESULT)
    assert verdict == "green"
    assert reason == ""


def test_finalize_without_schedule_reports_green_and_does_not_schedule(store):
    outcome = finalize_run(
        provider="generic",
        url="https://example.com",
        prompt="watch the page",
        result=GOOD_RESULT,
        store=store,
        now=FIXED_NOW,
    )
    assert isinstance(outcome, RunOutcome)
    assert outcome.verdict == "green"
    assert outcome.scheduled is False
    assert outcome.next_run_at is None
    assert store.list_tasks() == []


def test_failed_run_is_blocked_with_reason_and_not_scheduled(store):
    outcome = finalize_run(
        provider="generic",
        url="https://example.com",
        prompt="watch the page",
        result={"success": False, "error": "browser crashed"},
        schedule={"interval_minutes": 60},
        store=store,
        now=FIXED_NOW,
    )
    assert outcome.verdict == "blocked"
    assert outcome.scheduled is False
    assert "browser crashed" in outcome.reason
    assert store.list_tasks() == []


def test_success_without_evidence_is_blocked():
    verdict, reason = derive_verdict({"success": True, "goal": None, "session_recording": None})
    assert verdict == "blocked"
    assert "no goal or session recording" in reason


# --------------------------------------------------------------------------- #
# Enrollment
# --------------------------------------------------------------------------- #
def test_green_run_with_schedule_enrolls_with_human_arrival(store):
    outcome = finalize_run(
        provider="linkedin",
        url="https://www.linkedin.com/feed/",
        prompt="scroll the feed",
        result=GOOD_RESULT,
        schedule={"interval_minutes": 90, "arrival_seed": "test-arrival"},
        store=store,
        now=FIXED_NOW,
    )
    assert outcome.verdict == "green"
    assert outcome.scheduled is True
    assert outcome.interval_minutes == 90

    # Anti-fingerprint contract: the next arrival is jittered into the future,
    # NOT the exact nominal instant, never on the whole-second grid, and bounded.
    next_run = from_iso(outcome.next_run_at)
    nominal = FIXED_NOW + timedelta(minutes=90)
    assert next_run > FIXED_NOW
    assert next_run != nominal
    assert next_run.microsecond != 0
    assert FIXED_NOW < next_run < FIXED_NOW + timedelta(days=10)

    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.id == task_id_for("linkedin", "https://www.linkedin.com/feed/", "scroll the feed")
    assert task.provider == "linkedin"
    assert task.interval_minutes == 90
    assert task.last_verdict == "green"
    assert from_iso(task.next_run_at) == next_run
    # Latent timing state + circadian identity are persisted for the worker.
    assert task.arrival_state and task.arrival_state.get("seed")
    assert task.chronotype and task.timezone


def test_schedule_persona_and_timezone_drive_circadian_identity(store):
    """A scheduled task must inherit the *persona's* chronotype in the *requested*
    timezone, not the fleet-wide nine-to-five Eastern default. Without this the
    whole fleet shares one circadian arrival rhythm — a cross-session tell. The
    endpoint forwards body.persona + body.schedule_timezone into this seam."""
    outcome = finalize_run(
        provider="linkedin",
        url="https://www.linkedin.com/feed/",
        prompt="scroll the feed",
        result=GOOD_RESULT,
        schedule={
            "interval_minutes": 60,
            "persona": "curious_reader",  # -> "evening" chronotype
            "timezone": "Europe/Berlin",
        },
        store=store,
        now=FIXED_NOW,
    )
    assert outcome.scheduled is True
    task = store.list_tasks()[0]
    assert task.chronotype == "evening"
    assert task.timezone == "Europe/Berlin"

    # And the persisted identity survives a worker reschedule (run_due_tasks
    # rebuilds the profile from the record, not from a fresh default).
    later = FIXED_NOW + timedelta(minutes=61)
    run_due_tasks(store=store, runner=lambda *_: GOOD_RESULT, now=later)
    reloaded = store.list_tasks()[0]
    assert reloaded.chronotype == "evening"
    assert reloaded.timezone == "Europe/Berlin"


def test_schedule_without_persona_defaults_to_desk_worker(store):
    """No persona/timezone => the existing desk-worker default (nine_to_five,
    America/New_York), so the change is backward compatible."""
    finalize_run(
        provider="generic", url="https://example.com", prompt=None,
        result=GOOD_RESULT, schedule={"interval_minutes": 60}, store=store, now=FIXED_NOW,
    )
    task = store.list_tasks()[0]
    assert task.chronotype == "nine_to_five"
    assert task.timezone == "America/New_York"


def test_arrival_jitter_can_be_disabled_for_legacy_timing(store):
    scheduler.set_arrival_jitter(False)
    try:
        outcome = finalize_run(
            provider="generic",
            url="https://example.com",
            prompt=None,
            result=GOOD_RESULT,
            schedule={"interval_minutes": 60},
            store=store,
            now=FIXED_NOW,
        )
    finally:
        scheduler.set_arrival_jitter(True)
    assert from_iso(outcome.next_run_at) == FIXED_NOW + timedelta(minutes=60)


def test_check_interval_minutes_key_is_accepted(store):
    outcome = finalize_run(
        provider="generic",
        url="https://example.com",
        prompt=None,
        result=GOOD_RESULT,
        schedule={"check_interval_minutes": 30},
        store=store,
        now=FIXED_NOW,
    )
    assert outcome.scheduled is True
    assert outcome.interval_minutes == 30


def test_reenrolling_same_target_updates_in_place(store):
    finalize_run(
        provider="generic", url="https://example.com", prompt="p",
        result=GOOD_RESULT, schedule={"interval_minutes": 60}, store=store, now=FIXED_NOW,
    )
    finalize_run(
        provider="generic", url="https://example.com", prompt="p",
        result=GOOD_RESULT, schedule={"interval_minutes": 15}, store=store, now=FIXED_NOW,
    )
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].interval_minutes == 15


def test_interval_below_floor_is_clamped(store):
    outcome = finalize_run(
        provider="generic", url="https://example.com", prompt=None,
        result=GOOD_RESULT, schedule={"interval_minutes": 1}, store=store, now=FIXED_NOW,
    )
    assert outcome.interval_minutes == scheduler.MIN_INTERVAL_MINUTES


# --------------------------------------------------------------------------- #
# Due-task detection
# --------------------------------------------------------------------------- #
def test_due_task_detection(store):
    store.enroll(
        provider="generic", url="https://example.com", prompt=None,
        interval_minutes=60, now=FIXED_NOW,
    )
    # next_run_at is FIXED_NOW + 60m; not due just after enrollment.
    assert store.due_tasks(now=FIXED_NOW + timedelta(minutes=59)) == []
    # Due once the interval has elapsed.
    due = store.due_tasks(now=FIXED_NOW + timedelta(minutes=61))
    assert len(due) == 1


def test_disabled_task_is_never_due(store):
    rec = store.enroll(
        provider="generic", url="https://example.com", prompt=None,
        interval_minutes=60, now=FIXED_NOW,
    )
    store.set_enabled(rec.id, False, now=FIXED_NOW)
    assert store.due_tasks(now=FIXED_NOW + timedelta(hours=10)) == []


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def test_run_due_tasks_executes_and_reschedules(store):
    rec = store.enroll(
        provider="generic", url="https://example.com", prompt="p",
        interval_minutes=60, now=FIXED_NOW,
    )
    calls = []

    def fake_runner(provider, url, prompt):
        calls.append((provider, url, prompt))
        return GOOD_RESULT

    later = FIXED_NOW + timedelta(minutes=61)
    summaries = run_due_tasks(store=store, runner=fake_runner, now=later)

    assert calls == [("generic", "https://example.com", "p")]
    assert summaries == [
        {"task_id": rec.id, "provider": "generic", "url": "https://example.com", "verdict": "green"}
    ]
    updated = store.get(rec.id)
    assert updated.last_verdict == "green"
    assert from_iso(updated.last_run_at) == later
    # Rescheduled to a jittered, sub-second future instant (not exactly +60m,
    # not on the whole-second grid), and the latent timing state is carried over.
    next_run = from_iso(updated.next_run_at)
    assert next_run > later
    assert next_run != later + timedelta(minutes=60)
    assert next_run.microsecond != 0
    assert updated.arrival_state and updated.arrival_state.get("seed")


def test_run_due_tasks_marks_failed_run_blocked(store):
    rec = store.enroll(
        provider="generic", url="https://example.com", prompt=None,
        interval_minutes=60, now=FIXED_NOW,
    )

    def failing_runner(provider, url, prompt):
        return {"success": False, "error": "nope"}

    later = FIXED_NOW + timedelta(minutes=61)
    summaries = run_due_tasks(store=store, runner=failing_runner, now=later)
    assert summaries[0]["verdict"] == "blocked"
    assert store.get(rec.id).last_verdict == "blocked"


def test_run_due_tasks_survives_runner_exception(store):
    store.enroll(
        provider="generic", url="https://example.com", prompt=None,
        interval_minutes=60, now=FIXED_NOW,
    )

    def boom(provider, url, prompt):
        raise RuntimeError("kaboom")

    later = FIXED_NOW + timedelta(minutes=61)
    summaries = run_due_tasks(store=store, runner=boom, now=later)
    assert summaries[0]["verdict"] == "blocked"


def test_set_run_callable_used_by_default(store):
    seen = []

    def registered(provider, url, prompt):
        seen.append(url)
        return GOOD_RESULT

    store.enroll(
        provider="generic", url="https://example.com", prompt=None,
        interval_minutes=60, now=FIXED_NOW,
    )
    set_run_callable(registered)
    try:
        run_due_tasks(store=store, now=FIXED_NOW + timedelta(minutes=61))
    finally:
        set_run_callable(None)
    assert seen == ["https://example.com"]


def test_start_worker_runs_due_tasks_then_stops(store):
    store.enroll(
        provider="generic", url="https://example.com", prompt=None,
        interval_minutes=5, now=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    ran = []

    def runner(provider, url, prompt):
        ran.append(url)
        return GOOD_RESULT

    handle = start_worker(poll_seconds=0.05, store=store, runner=runner)
    try:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=3)
        while not ran and datetime.now(timezone.utc) < deadline:
            pass
    finally:
        handle.stop(timeout=2.0)
    assert ran, "worker should have executed the due task at least once"
    assert not handle.thread.is_alive()
