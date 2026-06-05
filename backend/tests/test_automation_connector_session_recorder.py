from __future__ import annotations

from imposter5.automation_connector.session_recorder import SessionRecorder


def test_session_recorder_stores_metadata() -> None:
    recorder = SessionRecorder(
        {
            "run_id": "run-1",
            "recorder": {"enabled": True, "max_events": 2},
            "analytics": {"synthetic": True, "labels": ["automation_connector", "provider:test"]},
        }
    )

    recorder.record("goto", metadata={"url": "https://example.com", "note": "first"})
    recorder.record("click", metadata={"selector": "button"})
    recorder.record("extra", metadata={"ignored": True})
    payload = recorder.payload()

    assert payload["run_id"] == "run-1"
    assert payload["event_count"] == 2
    assert payload["analytics"]["labels"] == ["automation_connector", "provider:test"]
    assert payload["events"][0]["metadata"]["url"] == "https://example.com"
    assert payload["events"][0]["metadata"]["note"] == "first"


def test_session_recorder_is_off_by_default() -> None:
    recorder = SessionRecorder({"run_id": "run-2"})

    recorder.record("goto", metadata={"url": "https://example.com"})
    payload = recorder.payload()

    assert recorder.enabled is False
    assert payload["event_count"] == 0
