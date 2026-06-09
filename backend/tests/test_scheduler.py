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
def test_green_run_with_schedule_enrolls_with_correct_next_run_at(store):
    outcome = finalize_run(
        provider="linkedin",
        url="https://www.linkedin.com/feed/",
        prompt="scroll the feed",
        result=GOOD_RESULT,
        schedule={"interval_minutes": 90},
        store=store,
        now=FIXED_NOW,
    )
    assert outcome.verdict == "green"
    assert outcome.scheduled is True
    assert outcome.interval_minutes == 90

    expected_next = FIXED_NOW + timedelta(minutes=90)
    assert from_iso(outcome.next_run_at) == expected_next

    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.id == task_id_for("linkedin", "https://www.linkedin.com/feed/", "scroll the feed")
    assert task.provider == "linkedin"
    assert task.interval_minutes == 90
    assert task.last_verdict == "green"
    assert from_iso(task.next_run_at) == expected_next


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
    # Rescheduled to one interval past the run time.
    assert from_iso(updated.next_run_at) == later + timedelta(minutes=60)


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
