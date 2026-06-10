"""Gauntlet journey entry point — now a thin adapter over the unified Story engine.

History: this module used to own its own multi-minute orchestration (an aimless
feed loop + a weighted excursion menu). That was a *second* session orchestrator
alongside ``story/`` (the goal-anchored scene compiler/executor), which produced
mechanism sprawl. The orchestration has been folded into the Story engine:

  - ``session_planner.build_feed_intent`` picks a per-session ARC (interrupted /
    quick-check / pure-feed / feed+lookup / research) and emits a feed-browse
    ``TaskIntent`` — this is the cross-session variety layer (no two sessions are
    the same routine).
  - ``story.compiler`` samples a concrete feed scene graph (variable length +
    woven check-stops) from that intent.
  - ``story.executor`` runs it, delegating the physical behavior to the proven,
    Blue-validated feed primitives in ``loaders.feed_actions`` (the exact code
    that scored HUMAN_EVADED).

This function remains the stable call surface for ``app.py`` and adapts the Story
result back into the journey-summary shape the product/playback UI already reads.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

from imposter5.automation_connector.session_recorder import SessionRecorder
from imposter5.story.executor import run_story
from imposter5.story.session_planner import build_feed_intent

logger = logging.getLogger(__name__)


def run_gauntlet_journey(
    page: Any,
    behavior_plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
    interest_terms: list[str] | None = None,
    duration_s: float = 240.0,
    seed: int | None = None,
) -> dict[str, Any]:
    """Drive a human-like feed-browse session across the gauntlet via the Story
    engine, and return a journey summary.

    Session variety comes from the arc the planner samples; physical behavior comes
    from the shared feed primitives. ``duration_s`` bounds the session and sizes the
    arc's scan target; omit ``seed`` for genuinely different sessions each run.
    """
    plan: dict[str, Any] = dict(behavior_plan or {})
    # The executor's feed session reads the time budget from the behavior plan.
    plan["gauntlet_duration_s"] = float(duration_s)
    if interest_terms:
        plan["interest_terms"] = interest_terms

    rng = random.Random(seed) if seed is not None else random.Random()
    intent, arc_name = build_feed_intent(plan, duration_s=duration_s, rng=rng)
    logger.info(
        "[gauntlet_journey] arc=%s scan_target=%s tangents<=%s duration=%.0fs",
        arc_name, intent.goal_predicate.target, intent.curiosity.max_tangents, duration_s,
    )

    start = time.monotonic()
    result = run_story(
        page,
        intent,
        seed=seed,
        recorder=recorder,
        behavior_plan=plan,
    )
    elapsed = round(time.monotonic() - start, 1)

    # Adapt the Story result into the journey-summary shape app.py / playback read.
    summary: dict[str, Any] = dict(result.get("feed_summary") or {})
    summary["duration_s"] = elapsed
    summary["arc"] = arc_name
    summary["behavior_driver"] = "story_feed_arc"
    summary["goal_met"] = result.get("goal_met")
    summary["story"] = {
        "goal": result.get("goal"),
        "scene_count": result.get("scene_count"),
        "executed": result.get("executed"),
        "tangents_fired": result.get("tangents_fired"),
        "tangents_returned": result.get("tangents_returned"),
        "resume_stack_balanced": result.get("resume_stack_balanced"),
        "archetype": intent.archetype,
    }
    if recorder is not None:
        try:
            summary["session_recording"] = recorder.payload()
        except Exception:
            summary["session_recording"] = None

    logger.info(
        "[gauntlet_journey] arc=%s done in %.1fs: scans=%d captured=%d profiles=%d "
        "lookups=%d notifs=%d glances=%d likes=%d goal_met=%s",
        arc_name, elapsed, summary.get("feed_scan_bursts", 0), summary.get("posts_captured", 0),
        summary.get("profiles_opened", 0), summary.get("lookups", 0),
        summary.get("notifications_visited", 0), summary.get("glances", 0),
        summary.get("likes", 0), summary.get("goal_met"),
    )
    return summary
