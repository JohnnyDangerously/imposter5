"""Session planner: turn a feed-browse campaign into a per-session TaskIntent so
that no two sessions look like the same daily routine.

The user's V1: "set a goal (browse the feed), random length, sprinkle in
check-stops (alerts / search / messaging / profile), and session 1 != session 2
!= session 3." That cross-session variety is realized here as a small library of
ARC ARCHETYPES. Each arc is a weighted shape — how long the session runs and which
check-stops it tends to weave in — and the planner samples one per session, then
samples *within* it (length, tangent budget) so even repeats of the same arc differ.

The arc only sets the INTENT knobs (scan target + curiosity budget + variance);
the StoryCompiler then samples a concrete scene graph and the StoryExecutor runs
it via the proven feed primitives. So variety compounds: arc choice -> intent
knobs -> compiled plan -> motor RNG.

This is deliberately small. It is NOT a planner DSL; it is the menu of human
session shapes a feed-browsing person actually exhibits.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from imposter5.story.task_intent import TaskIntent, parse_task_intent

# Rough wall-clock cost of one feed segment (a multi-step Markov scan burst +
# capture + content-driven dwell + occasional peek/like), measured at ~15-18s on
# the gauntlet. Used to size the scan target to the session's time budget so the
# arc finishes its goal inside the duration the operator asked for (honest
# goal_met) rather than getting cut off by the time guard. Excursions add time on
# top, so this stays slightly conservative.
_SECONDS_PER_SCAN = 16.0


@dataclass(frozen=True)
class FeedArc:
    """One human session shape for a feed-browse campaign."""

    name: str
    weight: float
    scan_frac: tuple[float, float]   # session length as a fraction of the time budget
    tangent_chance: float            # per-segment chance of a check-stop
    max_tangents: int                # cap on check-stops this session
    tangent_scenes: tuple[str, ...]  # which check-stops this arc tends to do
    dwell_cv: float
    describe: str
    # Binge arcs: (lo, hi) profiles to open in one sitting. None => an ordinary
    # feed-scroll arc whose length is driven by ``scan_frac``, not an open count.
    open_count: tuple[int, int] | None = None


# The base loop is always aimless feed scrolling; arcs differ in length and what
# they wander into. Weights keep ordinary browsing common but NOT dominant —
# lookup/research/check-stop sessions collectively outweigh pure scrolling, so a
# week of usage does not read as "scrolls between two values and does nothing else."
ARCS: tuple[FeedArc, ...] = (
    FeedArc(
        "micro_dip", 1.8, (0.0, 0.10), 0.85, 1,
        ("tangent_lookup", "tangent_glance"), 0.30,
        "Pop in, check one profile or glance, leave — barely touches the feed.",
    ),
    FeedArc(
        "interrupted", 1.0, (0.08, 0.22), 0.0, 0, (), 0.40,
        "A few stops down the feed, then interrupted — got pulled away.",
    ),
    FeedArc(
        "quick_check", 1.6, (0.18, 0.40), 0.55, 1,
        ("tangent_notifications", "tangent_glance"), 0.40,
        "Check alerts (or a quick glance), a little scroll, done.",
    ),
    FeedArc(
        "pure_feed", 1.8, (0.55, 1.0), 0.35, 2,
        ("tangent_notifications", "tangent_glance", "tangent_lookup"), 0.45,
        "Browsing the feed for a while, weaving in a couple of check-stops "
        "(alerts, a glance, or looking someone up) — not an unbroken scroll.",
    ),
    FeedArc(
        "feed_lookup", 2.4, (0.45, 0.85), 0.45, 2,
        ("tangent_lookup", "tangent_notifications", "tangent_glance"), 0.40,
        "Scroll, look someone up, back to the feed, scroll more.",
    ),
    FeedArc(
        "research", 1.8, (0.60, 1.0), 0.5, 3,
        ("tangent_search", "tangent_lookup", "tangent_notifications", "tangent_glance"), 0.40,
        "Scroll, search an interest and read a profile, return, keep scrolling.",
    ),
    # Profile-binge arcs: the MAIN activity is opening many profiles fast, not feed
    # scrolling — modelling "I'll look at ~5 quickly and leave" and "I'll look at
    # 15-30 and it won't take long." Curiosity is off: a binge is focused, not wandering.
    FeedArc(
        "five_and_bounce", 1.2, (0.0, 0.18), 0.0, 0, (), 0.35,
        "Open a handful of profiles fast — a quick look at each — then bounce.",
        open_count=(4, 7),
    ),
    FeedArc(
        "profile_sweep", 1.0, (0.0, 0.25), 0.0, 0, (), 0.30,
        "Sweep through many profiles in one sitting, skimming each, rarely lingering.",
        open_count=(15, 30),
    ),
)
_ARCS_BY_NAME = {a.name: a for a in ARCS}


def _pick_arc(plan: dict[str, Any] | None, rng: random.Random) -> FeedArc:
    """Choose an arc: explicit hint > app-loaded value tree > weighted random."""
    if isinstance(plan, dict):
        hinted = plan.get("arc") or plan.get("session_arc")
        if isinstance(hinted, str) and hinted in _ARCS_BY_NAME:
            return _ARCS_BY_NAME[hinted]
        # App loaded specific people to check -> a purposeful lookup/research arc.
        if plan.get("lookup_people"):
            return _ARCS_BY_NAME["research" if rng.random() < 0.4 else "feed_lookup"]
        if plan.get("long_browse"):
            return _ARCS_BY_NAME["pure_feed"]
    weights = [a.weight for a in ARCS]
    return rng.choices(ARCS, weights=weights, k=1)[0]


def _build_binge_intent(
    arc: FeedArc, plan: dict[str, Any] | None, rng: random.Random
) -> TaskIntent:
    """A profile-FIRST session: search, then open ``open_count`` profiles, skimming
    each (open -> back, no long read). This is the binge shape a feed-scroll arc
    cannot express — the compiler now honors the open_count target instead of the
    old hard 1-3 cap, so 15-30 opens actually happen.
    """
    assert arc.open_count is not None
    lo, hi = arc.open_count
    open_target = rng.randint(int(lo), int(hi))
    site = (plan or {}).get("site", "gauntlet") if isinstance(plan, dict) else "gauntlet"
    payload: dict[str, Any] = {
        "schema": "lhhl-task-intent/v1",
        "site": site,
        "archetype": f"profile_browse:{arc.name}",
        "describe": arc.describe,
        "objective": {
            # open -> back per profile (no profile_read): a fast skim, not a deep read.
            "main_scenes": [
                "search_open", "search_query", "results_scan", "profile_open", "profile_back",
            ],
            "query_hint": "",
            "goal_predicate": {"type": "open_count", "target": open_target, "jitter": 0.3},
        },
        "curiosity": {
            "tangent_chance": arc.tangent_chance,
            "max_tangents": arc.max_tangents,
            "max_depth": 1,
            "tangent_scenes": list(arc.tangent_scenes),
        },
        "variance": {"dwell_cv": arc.dwell_cv, "order_jitter": True, "partial_substitution": True},
        "cadence": "profile_browse",
    }
    return parse_task_intent(payload)


def build_feed_intent(
    plan: dict[str, Any] | None,
    *,
    duration_s: float = 240.0,
    rng: random.Random | None = None,
    arc_name: str | None = None,
) -> tuple[TaskIntent, str]:
    """Sample one feed-browse TaskIntent for this session. Returns (intent, arc_name)."""
    rng = rng or random.Random()
    if arc_name and arc_name in _ARCS_BY_NAME:
        arc = _ARCS_BY_NAME[arc_name]
    else:
        arc = _pick_arc(plan, rng)

    if arc.open_count is not None:
        return _build_binge_intent(arc, plan, rng), arc.name

    max_scans = max(2, int(round(float(duration_s) / _SECONDS_PER_SCAN)))
    frac = rng.uniform(*arc.scan_frac)
    scan_target = max(1, int(round(frac * max_scans)))

    # Enough lookups requested that we should guarantee the budget can drain them.
    requested_lookups = 0
    if isinstance(plan, dict) and isinstance(plan.get("lookup_people"), (list, tuple)):
        requested_lookups = len([p for p in plan["lookup_people"] if str(p).strip()])
    max_tangents = arc.max_tangents
    tangent_scenes = arc.tangent_scenes
    if requested_lookups and "tangent_lookup" not in tangent_scenes:
        tangent_scenes = ("tangent_lookup",) + tangent_scenes
    if requested_lookups:
        max_tangents = max(max_tangents, requested_lookups + 1)

    payload: dict[str, Any] = {
        "schema": "lhhl-task-intent/v1",
        "site": (plan or {}).get("site", "gauntlet") if isinstance(plan, dict) else "gauntlet",
        "archetype": f"feed_browse:{arc.name}",
        "describe": arc.describe,
        "objective": {
            "main_scenes": ["feed_scan"],
            "query_hint": "",
            "goal_predicate": {"type": "scan_count", "target": scan_target, "jitter": 0.25},
        },
        "curiosity": {
            "tangent_chance": arc.tangent_chance,
            "max_tangents": max_tangents,
            "max_depth": 1,
            "tangent_scenes": list(tangent_scenes),
        },
        "variance": {"dwell_cv": arc.dwell_cv, "order_jitter": True, "partial_substitution": True},
        "cadence": "feed_browse",
    }
    return parse_task_intent(payload), arc.name
