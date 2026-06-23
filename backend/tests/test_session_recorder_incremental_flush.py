"""Durability of the session recorder's on-disk event log.

The recorder used to keep its event track in memory only and rely on a single
end-of-run write (``app._write_session_sidecar`` -> ``<movie>.session.json``).
That dropped the whole track whenever the run was interrupted before the browser
context closed, or returned zero posts so the payload was never lifted — the
"video with no sidecar" and "sidecar with 0 events" orphans seen in the gauntlet
outputs.

These tests pin the fix: when a record dir is supplied the recorder mirrors
every event to ``events.jsonl`` the instant it happens, and ``load_partial_session``
recovers that log when the in-memory payload never made it through (the
recovery the backend finalize path falls back to).
"""
from __future__ import annotations

import json
from pathlib import Path

from imposter5.automation_connector.session_recorder import (
    EVENTS_JSONL_NAME,
    EVENTS_META_NAME,
    SessionRecorder,
    load_partial_session,
)


def _enabled_plan(**extra) -> dict:
    plan = {"recorder": {"enabled": True, "max_events": 50}, "run_id": "run-xyz"}
    plan.update(extra)
    return plan


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_recorder_appends_each_event_to_jsonl(tmp_path: Path) -> None:
    rec = SessionRecorder(_enabled_plan(), flush_dir=tmp_path)
    rec.record("mouse_move", metadata={"x": 10, "y": 20})
    rec.record("scroll", metadata={"delta_y": 240})
    rec.record("feed_capture", metadata={"author": "Ada"})

    log = tmp_path / EVENTS_JSONL_NAME
    assert log.exists()
    lines = _read_lines(log)
    assert [e["action"] for e in lines] == ["mouse_move", "scroll", "feed_capture"]
    assert [e["index"] for e in lines] == [0, 1, 2]
    assert lines[0]["metadata"]["x"] == 10

    meta = json.loads((tmp_path / EVENTS_META_NAME).read_text(encoding="utf-8"))
    assert meta["run_id"] == "run-xyz"
    assert meta["max_events"] == 50


def test_recorder_jsonl_grows_incrementally(tmp_path: Path) -> None:
    # Each event lands on disk immediately, so a kill mid-run keeps the track.
    rec = SessionRecorder(_enabled_plan(), flush_dir=tmp_path)
    log = tmp_path / EVENTS_JSONL_NAME

    rec.record("a")
    assert len(_read_lines(log)) == 1
    rec.record("b")
    assert len(_read_lines(log)) == 2


def test_recorder_no_flush_without_dir(tmp_path: Path) -> None:
    rec = SessionRecorder(_enabled_plan())  # no flush_dir
    rec.record("mouse_move")
    assert not (tmp_path / EVENTS_JSONL_NAME).exists()
    assert rec.payload()["event_count"] == 1  # in-memory path unchanged


def test_recorder_disabled_writes_nothing(tmp_path: Path) -> None:
    rec = SessionRecorder({"recorder": {"enabled": False}}, flush_dir=tmp_path)
    rec.record("mouse_move")
    assert not (tmp_path / EVENTS_JSONL_NAME).exists()
    assert not (tmp_path / EVENTS_META_NAME).exists()


def test_recorder_respects_max_events_on_disk(tmp_path: Path) -> None:
    rec = SessionRecorder({"recorder": {"enabled": True, "max_events": 2}}, flush_dir=tmp_path)
    for i in range(5):
        rec.record(f"e{i}")
    assert len(_read_lines(tmp_path / EVENTS_JSONL_NAME)) == 2


def test_load_partial_recovers_events_from_jsonl(tmp_path: Path) -> None:
    # Simulate an interrupted/zero-post run: a live log on disk, but the
    # in-memory recording payload never reached finalize.
    rec = SessionRecorder(_enabled_plan(), flush_dir=tmp_path)
    rec.record("mouse_move", metadata={"x": 1, "y": 2})
    rec.record("scroll", metadata={"delta_y": 300})

    recovered = load_partial_session(tmp_path)

    assert recovered is not None
    assert recovered["recovered_from_partial_log"] is True
    assert recovered["run_id"] == "run-xyz"
    assert recovered["event_count"] == 2
    assert [e["action"] for e in recovered["events"]] == ["mouse_move", "scroll"]


def test_load_partial_returns_none_without_log(tmp_path: Path) -> None:
    # No events.jsonl at all (recording never started / no flush dir): nothing to
    # recover, so finalize keeps writing whatever the in-memory payload had.
    assert load_partial_session(tmp_path) is None


def test_load_partial_returns_none_for_empty_log(tmp_path: Path) -> None:
    # An empty file is not a recoverable track.
    (tmp_path / EVENTS_JSONL_NAME).write_text("", encoding="utf-8")
    assert load_partial_session(tmp_path) is None


def test_load_partial_tolerates_truncated_tail_line(tmp_path: Path) -> None:
    # A hard kill can truncate the final JSON line mid-write; the complete lines
    # before it must still be recovered.
    log = tmp_path / EVENTS_JSONL_NAME
    good = json.dumps({"index": 0, "action": "mouse_move", "metadata": {}})
    log.write_text(good + "\n" + '{"index": 1, "action": "scr', encoding="utf-8")

    recovered = load_partial_session(tmp_path)
    assert recovered is not None
    assert recovered["event_count"] == 1
    assert recovered["events"][0]["action"] == "mouse_move"
