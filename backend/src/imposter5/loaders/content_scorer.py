"""Content scorer — decide how a persona's attention reacts to feed posts.

This is what turns "scroll past everything at the same speed" into genuine
selective attention: given the text we capture as we browse, score each post for
how interesting it is to a specific persona and what a human would *do* about it
(skip / dwell / highlight / click). The behavior layer then dwells longer,
traces a sentence, or opens the people/posts that actually matter.

Two backends:
  - ``heuristic`` (default): instant, dependency-free signal scoring. Good
    enough to make behavior content-shaped with zero latency or external calls.
  - ``llm``: batch-score via the ``llm-query`` broker (real judgment — the spike
    showed it cleanly separates relevant technical/peer content from motivational
    noise, recruiter spam, and engagement bait). It is SLOW (~10-18s/screen), so
    it runs in a background thread: submit captured posts, keep browsing, and
    consume scores once they land. The motion loop never waits on a model.

Select the backend with ``IMPOSTER5_CONTENT_EVAL`` (``heuristic`` | ``llm`` |
``off``; default ``heuristic``) and the model with ``IMPOSTER5_CONTENT_EVAL_MODEL``
(default ``gpt``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from typing import Any

logger = logging.getLogger(__name__)

_ACTIONS = ("skip", "dwell", "highlight", "click")

# Signal vocabulary for the heuristic backend. These are the kinds of things a
# data/eng-leaning professional slows down for vs scrolls past.
_POSITIVE = (
    "data", "pipeline", "ml", "platform", "infra", "observability", "reliability",
    "open-source", "open source", "benchmark", "post-mortem", "postmortem",
    "orchestration", "lineage", "feature store", "lakehouse", "iceberg", "airflow",
    "hiring", "engineer", "architect", "head of", "vp ", "series ",
)
_NEGATIVE = (
    "hustle", "mindset", "grateful", "humbled", "blessed", "motivational",
    "change my mind", "workiversary", "anniversary", "avocado", "nomad",
    "intern", "giveaway", "🙏", "🎉",
)


def _persona_label(persona: str | None) -> str:
    return persona or (
        "a senior data/ML platform leader who hires engineers, cares about "
        "reliability/observability/tooling, checks on peers and ex-colleagues, "
        "and ignores motivational fluff and recruiter spam"
    )


def _heuristic_score(text: str) -> dict[str, Any]:
    low = (text or "").lower()
    score = 0.15
    for kw in _POSITIVE:
        if kw in low:
            score += 0.18
    for kw in _NEGATIVE:
        if kw in low:
            score -= 0.22
    if "?" in text and len(text) < 80:  # short question = engagement bait
        score -= 0.1
    score = max(0.0, min(1.0, score))
    if score >= 0.8:
        action = "click"
    elif score >= 0.6:
        action = "highlight"
    elif score >= 0.4:
        action = "dwell"
    else:
        action = "skip"
    return {"interest": round(score, 2), "action": action, "why": "heuristic signal match"}


def _build_llm_prompt(batch: list[dict[str, Any]], persona: str | None) -> str:
    lines = [
        "You are simulating the moment-to-moment attention of this person scrolling "
        "a LinkedIn-style feed.",
        f'Persona: "{_persona_label(persona)}".',
        "",
        "For EACH numbered post, decide how their attention behaves. Return ONLY a "
        "compact JSON array of objects with fields: n (int, the post number), "
        "interest (0.0-1.0), action (one of skip/dwell/highlight/click), why (<=8 words).",
        "Be selective like a real busy person: most posts are \"skip\". Only "
        "genuinely relevant items get dwell/highlight/click.",
        "",
        "FEED:",
    ]
    for i, p in enumerate(batch, start=1):
        txt = (p.get("text") or p.get("headline") or "").strip().replace("\n", " ")
        lines.append(f'{i}. "{txt[:240]}"')
    lines.append("")
    lines.append("Return only the JSON array, nothing else.")
    return "\n".join(lines)


def _parse_llm_json(stdout: str) -> list[dict[str, Any]]:
    """Pull the JSON array out of llm-query's markdown-wrapped response."""
    # The broker wraps the answer in markdown; grab the outermost [ ... ] array.
    start = stdout.find("[")
    end = stdout.rfind("]")
    if start < 0 or end <= start:
        return []
    blob = stdout[start : end + 1]
    try:
        data = json.loads(blob)
    except Exception:
        # Tolerate trailing prose after the array by trimming to a balanced bracket.
        depth = 0
        cut = None
        for idx, ch in enumerate(blob):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    cut = idx + 1
                    break
        if cut is None:
            return []
        try:
            data = json.loads(blob[:cut])
        except Exception:
            return []
    return data if isinstance(data, list) else []


def _score_batch_via_llm_query(
    batch: list[dict[str, Any]], persona: str | None, model: str, timeout_s: float
) -> dict[str, dict[str, Any]]:
    """Blocking call to the llm-query broker; returns {post_id: score}."""
    prompt = _build_llm_prompt(batch, persona)
    try:
        proc = subprocess.run(
            ["llm-query", "ask", prompt, "--models", model, "--profile", "quick"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except Exception:
        logger.debug("[content_scorer] llm-query invocation failed", exc_info=True)
        return {}
    rows = _parse_llm_json(proc.stdout or "")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            n = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        if not (1 <= n <= len(batch)):
            continue
        pid = batch[n - 1].get("id")
        if not pid:
            continue
        action = str(row.get("action", "skip")).lower()
        if action not in _ACTIONS:
            action = "skip"
        try:
            interest = max(0.0, min(1.0, float(row.get("interest", 0.0))))
        except (TypeError, ValueError):
            interest = 0.0
        out[pid] = {"interest": round(interest, 2), "action": action, "why": str(row.get("why", ""))[:60]}
    return out


class ContentScorer:
    """Fire-and-forget feed scorer. Submit captured posts; read scores when ready."""

    def __init__(
        self,
        persona: str | None = None,
        *,
        backend: str | None = None,
        model: str | None = None,
        min_batch: int = 8,
    ) -> None:
        self.persona = persona
        self.backend = (backend or os.getenv("IMPOSTER5_CONTENT_EVAL", "heuristic")).lower()
        self.model = model or os.getenv("IMPOSTER5_CONTENT_EVAL_MODEL", "gpt")
        self.min_batch = min_batch
        self._scores: dict[str, dict[str, Any]] = {}
        self._seen: set[str] = set()
        self._unscored: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return self.backend in ("heuristic", "llm")

    def submit(self, posts: list[dict[str, Any]]) -> None:
        """Register newly captured posts for scoring (idempotent by post id)."""
        if not self.enabled or not posts:
            return
        fresh = []
        with self._lock:
            for p in posts:
                pid = p.get("id")
                if not pid or pid in self._seen:
                    continue
                self._seen.add(pid)
                fresh.append(p)
        if not fresh:
            return
        if self.backend == "heuristic":
            with self._lock:
                for p in fresh:
                    self._scores[p["id"]] = _heuristic_score(p.get("text") or p.get("headline") or "")
            return
        # llm backend: buffer and kick a background scoring pass when enough accrue.
        with self._lock:
            self._unscored.extend(fresh)
            ready = len(self._unscored) >= self.min_batch and (self._thread is None or not self._thread.is_alive())
            if ready:
                batch = self._unscored
                self._unscored = []
                self._thread = threading.Thread(target=self._score_thread, args=(batch,), daemon=True)
                self._thread.start()

    def _score_thread(self, batch: list[dict[str, Any]]) -> None:
        try:
            scores = _score_batch_via_llm_query(batch, self.persona, self.model, timeout_s=60.0)
        except Exception:
            logger.debug("[content_scorer] background scoring failed", exc_info=True)
            scores = {}
        # Fall back to heuristic for anything the model didn't return.
        with self._lock:
            self.calls += 1
            for p in batch:
                pid = p.get("id")
                if not pid:
                    continue
                self._scores[pid] = scores.get(pid) or _heuristic_score(p.get("text") or "")

    def score_for(self, post_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._scores.get(post_id)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            scored = list(self._scores.values())
        clicks = sum(1 for s in scored if s.get("action") == "click")
        return {
            "backend": self.backend,
            "model": self.model if self.backend == "llm" else None,
            "llm_calls": self.calls,
            "scored": len(scored),
            "high_interest": sum(1 for s in scored if s.get("interest", 0) >= 0.6),
            "click_worthy": clicks,
        }
