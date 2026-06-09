"""Durable store for enrolled recurring Imposter5 runs (workstream D).

A run that comes back ``green`` and was asked to repeat is enrolled here so a
worker can re-launch it on its cadence. The store is intentionally
dependency-light: a single JSON file under a configurable path, read/modified/
written under a process-local lock. That is plenty for the low write volume of
"a handful of recurring red-team targets" and avoids dragging in sqlite or a
server.

Record shape (one entry per recurring task)::

    {
        "id":              stable hash of (provider, url, prompt),
        "provider":        "linkedin" | "generic" | ...,
        "url":             target URL,
        "prompt":          natural-language prompt or null,
        "interval_minutes": cadence in minutes,
        "next_run_at":     ISO-8601 UTC timestamp the task next becomes due,
        "last_verdict":    verdict from the most recent execution ("green"/"blocked"),
        "last_run_at":     ISO-8601 UTC timestamp of the most recent execution, or null,
        "enabled":         bool — disabled tasks are never due,
        "created_at":      ISO-8601 UTC timestamp of first enrollment,
        "updated_at":      ISO-8601 UTC timestamp of the last mutation,
    }

Default file location resolution order:

1. ``IMPOSTER5_TASK_STORE_PATH`` environment variable, if set.
2. ``~/.imposter5/automation_tasks.json``.

Tests should construct ``TaskStore(path=<tmp path>)`` directly (or set the env
var) so they never touch the real location.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    """Timezone-aware "now" in UTC. Patch this in tests for determinism."""
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Serialize a datetime to a normalized ISO-8601 UTC string."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def from_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp back into a timezone-aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def task_id_for(provider: str, url: str, prompt: str | None) -> str:
    """Stable id for a target so re-enrolling the same target updates in place."""
    raw = "\x00".join([provider or "", url or "", prompt or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def default_store_path() -> Path:
    """Resolve the durable store path from the environment or the default home dir."""
    override = os.environ.get("IMPOSTER5_TASK_STORE_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".imposter5" / "automation_tasks.json"


@dataclass
class TaskRecord:
    """A single enrolled recurring task."""

    id: str
    provider: str
    url: str
    prompt: str | None
    interval_minutes: int
    next_run_at: str
    created_at: str
    updated_at: str
    last_verdict: str = "green"
    last_run_at: str | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskRecord":
        known = {f: raw.get(f) for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**known)

    def is_due(self, *, now: datetime | None = None) -> bool:
        """True when this task is enabled and its next_run_at has passed."""
        if not self.enabled:
            return False
        moment = now or utcnow()
        return from_iso(self.next_run_at) <= moment


class TaskStore:
    """JSON-file-backed store of enrolled recurring tasks.

    Thread-safe within a process via a re-entrant lock around every read-modify-
    write. Cross-process safety is not a goal (a single worker owns the cadence).
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        self._lock = threading.RLock()

    # -- persistence -------------------------------------------------------
    def _read_all(self) -> list[TaskRecord]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        records: list[TaskRecord] = []
        for item in raw:
            if isinstance(item, dict) and item.get("id"):
                try:
                    records.append(TaskRecord.from_dict(item))
                except TypeError:
                    continue
        return records

    def _write_all(self, records: list[TaskRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([r.to_dict() for r in records], indent=2, sort_keys=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)  # atomic swap so a crash never truncates the store

    # -- queries -----------------------------------------------------------
    def list_tasks(self) -> list[TaskRecord]:
        with self._lock:
            return self._read_all()

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            for record in self._read_all():
                if record.id == task_id:
                    return record
        return None

    def due_tasks(self, *, now: datetime | None = None) -> list[TaskRecord]:
        """All enabled tasks whose next_run_at is at or before ``now``."""
        moment = now or utcnow()
        with self._lock:
            return [r for r in self._read_all() if r.is_due(now=moment)]

    # -- mutations ---------------------------------------------------------
    def enroll(
        self,
        *,
        provider: str,
        url: str,
        prompt: str | None,
        interval_minutes: int,
        last_verdict: str = "green",
        now: datetime | None = None,
    ) -> TaskRecord:
        """Insert or update a recurring task; first run already happened, so the
        next due time is ``now + interval_minutes``."""
        moment = now or utcnow()
        next_run = moment + timedelta(minutes=interval_minutes)
        tid = task_id_for(provider, url, prompt)
        with self._lock:
            records = self._read_all()
            existing = next((r for r in records if r.id == tid), None)
            if existing is not None:
                existing.provider = provider
                existing.url = url
                existing.prompt = prompt
                existing.interval_minutes = interval_minutes
                existing.next_run_at = to_iso(next_run)
                existing.last_verdict = last_verdict
                existing.enabled = True
                existing.updated_at = to_iso(moment)
                result = existing
            else:
                result = TaskRecord(
                    id=tid,
                    provider=provider,
                    url=url,
                    prompt=prompt,
                    interval_minutes=interval_minutes,
                    next_run_at=to_iso(next_run),
                    created_at=to_iso(moment),
                    updated_at=to_iso(moment),
                    last_verdict=last_verdict,
                    last_run_at=None,
                    enabled=True,
                )
                records.append(result)
            self._write_all(records)
            return result

    def mark_ran(
        self,
        task_id: str,
        *,
        verdict: str,
        now: datetime | None = None,
    ) -> TaskRecord | None:
        """Record an execution: stamp last_run_at/last_verdict and reschedule."""
        moment = now or utcnow()
        with self._lock:
            records = self._read_all()
            target = next((r for r in records if r.id == task_id), None)
            if target is None:
                return None
            target.last_run_at = to_iso(moment)
            target.last_verdict = verdict
            target.next_run_at = to_iso(moment + timedelta(minutes=target.interval_minutes))
            target.updated_at = to_iso(moment)
            self._write_all(records)
            return target

    def set_enabled(self, task_id: str, enabled: bool, *, now: datetime | None = None) -> TaskRecord | None:
        moment = now or utcnow()
        with self._lock:
            records = self._read_all()
            target = next((r for r in records if r.id == task_id), None)
            if target is None:
                return None
            target.enabled = enabled
            target.updated_at = to_iso(moment)
            self._write_all(records)
            return target

    def remove(self, task_id: str) -> bool:
        with self._lock:
            records = self._read_all()
            kept = [r for r in records if r.id != task_id]
            if len(kept) == len(records):
                return False
            self._write_all(kept)
            return True
