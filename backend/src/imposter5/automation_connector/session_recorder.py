"""Bounded automation session recording.

The recorder stores action metadata and timing for one run, bounded by an event
cap. It is opt-in (dev/test) and off by default.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any


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

    def __init__(self, behavior_plan: dict[str, Any] | None = None) -> None:
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
        self._events.append(
            SessionEvent(
                index=len(self._events),
                action=str(action),
                status=str(status),
                label=str(label or action),
                elapsed_ms=round((time.monotonic() - self._started) * 1000),
                metadata=_safe_metadata(metadata or {}),
            )
        )

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
