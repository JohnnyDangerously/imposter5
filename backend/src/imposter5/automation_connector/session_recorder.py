"""Bounded automation session recording.

The recorder stores action metadata and timing for one run, bounded by an event
cap. It is opt-in (dev/test) and off by default.

Durability
----------
The in-memory event log was historically only persisted at the *very end* of a
run, when ``finalize_session_replay`` lifts ``recorder.payload()`` into the
``<movie>.session.json`` sidecar. That single end-of-run write has three ways to
silently drop every event:

* the process is interrupted (deploy / launchd respawn / SIGKILL) before the
  browser context closes, so no ``.webm`` is flushed and finalize has nothing to
  attach a sidecar to — leaving a video-less or sidecar-less orphan dir;
* the run returns zero posts, so the recorder payload (smuggled through each
  returned post's ``extraction_meta``) is never lifted, and the sidecar is
  written with ``events: []`` even though the recorder collected a full track;
* finalize itself races a kill between flushing the video and writing the
  sidecar.

When given a ``flush_dir`` the recorder *also* appends every event to
``<flush_dir>/events.jsonl`` the instant it happens (plus a one-shot
``events.meta.json`` header). That on-disk log survives an interrupted run and
is the source of truth a live viewer can tail and that finalize can fall back to
when the in-memory payload never made it through.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Append-only event log written next to the recorded video as it happens.
EVENTS_JSONL_NAME = "events.jsonl"
#: One-shot run header (run_id / bounds / wall-clock start) for the live log.
EVENTS_META_NAME = "events.meta.json"


# Generous bounds so recorded sessions can be inspected in full while still
# capping pathological payloads.
_MAX_STR_CHARS = 8_000
_MAX_LIST_ITEMS = 500


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR_CHARS else f"{value[:_MAX_STR_CHARS - 3]}..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:_MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        return _safe_metadata(value)
    return str(value)[:_MAX_STR_CHARS]


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _safe_value(value) for key, value in metadata.items()}


@dataclass(frozen=True)
class SessionEvent:
    index: int
    action: str
    status: str
    label: str
    elapsed_ms: int
    metadata: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "status": self.status,
            "label": self.label,
            "elapsed_ms": self.elapsed_ms,
            "metadata": self.metadata,
        }


class SessionRecorder:
    """Collect a bounded action log for one automation run."""

    def __init__(
        self,
        behavior_plan: dict[str, Any] | None = None,
        *,
        flush_dir: str | Path | None = None,
    ) -> None:
        plan = behavior_plan if isinstance(behavior_plan, dict) else {}
        recorder_plan = plan.get("recorder") if isinstance(plan.get("recorder"), dict) else {}
        self.enabled = bool(recorder_plan.get("enabled", False))
        try:
            self.max_events = max(1, min(500, int(recorder_plan.get("max_events", 160))))
        except (TypeError, ValueError):
            self.max_events = 160
        self.run_id = str(plan.get("run_id") or "")
        self.analytics = plan.get("analytics") if isinstance(plan.get("analytics"), dict) else {}
        self._started = time.monotonic()
        self._events: list[SessionEvent] = []
        # When a record dir is supplied, mirror events to disk as they happen so
        # an interrupted run keeps a partial track instead of losing everything.
        self._flush_path: Path | None = None
        if self.enabled and flush_dir:
            self._flush_path = Path(flush_dir) / EVENTS_JSONL_NAME
            self._write_meta(Path(flush_dir))

    def _write_meta(self, flush_dir: Path) -> None:
        """Write the one-shot run header beside the live event log (best-effort)."""
        try:
            flush_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "run_id": self.run_id,
                "max_events": self.max_events,
                "started_epoch": time.time(),
                "analytics": {
                    "synthetic": bool(self.analytics.get("synthetic", True)),
                    "labels": self.analytics.get("labels")
                    if isinstance(self.analytics.get("labels"), list)
                    else [],
                },
            }
            (flush_dir / EVENTS_META_NAME).write_text(json.dumps(meta), encoding="utf-8")
        except Exception as exc:  # pragma: no cover - disk/permission errors
            logger.debug("[session_recorder] could not write events meta: %s", exc)

    def _append_event_line(self, event: SessionEvent) -> None:
        """Append one event as a JSON line (best-effort, never raises into a run).

        Opens / flushes per event so a hard kill mid-run still leaves a complete
        log up to the last event — no buffered handle to lose.
        """
        if self._flush_path is None:
            return
        try:
            with open(self._flush_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_payload()) + "\n")
        except Exception as exc:  # pragma: no cover - disk/permission errors
            logger.debug("[session_recorder] could not append event line: %s", exc)

    @property
    def started_monotonic(self) -> float:
        """``time.monotonic()`` value captured when this recorder was created.

        ``elapsed_ms`` on every event is measured relative to this instant. The
        playback player needs it to align event time against the video clock,
        which starts at a different moment (page/context creation), so callers
        can compute the offset between the two timelines.
        """
        return self._started

    def record(
        self,
        action: str,
        *,
        status: str = "ok",
        label: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one sanitized event if recording is enabled."""
        if not self.enabled or len(self._events) >= self.max_events:
            return
        event = SessionEvent(
            index=len(self._events),
            action=str(action),
            status=str(status),
            label=str(label or action),
            elapsed_ms=round((time.monotonic() - self._started) * 1000),
            metadata=_safe_metadata(metadata or {}),
        )
        self._events.append(event)
        self._append_event_line(event)

    def payload(self) -> dict[str, Any]:
        labels = self.analytics.get("labels") if isinstance(self.analytics.get("labels"), list) else []
        return {
            "run_id": self.run_id,
            "enabled": self.enabled,
            "event_count": len(self._events),
            "max_events": self.max_events,
            "analytics": {
                "synthetic": bool(self.analytics.get("synthetic", True)),
                "labels": labels,
            },
            "events": [event.to_payload() for event in self._events],
        }


def load_partial_session(record_dir: str | Path) -> dict[str, Any] | None:
    """Rebuild a ``payload()``-shaped recording from the on-disk live event log.

    Symmetric with the per-event mirroring above: the recorder *writes*
    ``events.jsonl`` (+ ``events.meta.json``) as a run happens, and this *reads*
    it back. Finalize falls back to this when the in-memory payload never made it
    through — a zero-post run that never lifts it, or a kill that races the
    handoff — so the sidecar carries the real motor track instead of an empty/
    absent one. Returns None when there is no usable on-disk log.
    """
    log_path = Path(record_dir) / EVENTS_JSONL_NAME
    if not log_path.exists():
        return None
    events: list[dict[str, Any]] = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # A kill can truncate the final line mid-write; keep the rest.
                    continue
    except OSError:  # pragma: no cover - disk errors
        return None
    if not events:
        return None
    meta: dict[str, Any] = {}
    try:
        meta = json.loads((Path(record_dir) / EVENTS_META_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    analytics = meta.get("analytics") if isinstance(meta.get("analytics"), dict) else {}
    return {
        "run_id": str(meta.get("run_id") or ""),
        "enabled": True,
        "event_count": len(events),
        "max_events": int(meta.get("max_events") or len(events)),
        "analytics": {
            "synthetic": bool(analytics.get("synthetic", True)),
            "labels": analytics.get("labels") if isinstance(analytics.get("labels"), list) else [],
        },
        "events": events,
        "recovered_from_partial_log": True,
    }
