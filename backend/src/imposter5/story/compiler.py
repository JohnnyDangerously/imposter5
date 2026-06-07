"""StoryCompiler: turn a TaskIntent (+ optional affordance map) into a freshly
SAMPLED StoryPlan — a concrete, ordered scene graph with curiosity tangents.

What makes "no two attempts alike, yet each accomplishes the goal":

- A dependency-respecting MAIN backbone is always emitted (search before query
  before scan before profile-open ...), so the goal is ALWAYS reachable.
- ORDER JITTER randomly interleaves scan units and profile cycles (still keeping at
  least one scan before the first profile open).
- PARTIAL SUBSTITUTION varies how many scan units / profile cycles run and which
  optional scenes (read / back) are included per cycle.
- VARIABLE DWELL samples each scene's dwell from a right-skewed log-normal scaled by
  ``variance.dwell_cv``.
- CURIOSITY TANGENTS: at eligible scenes a tangent fires with ``tangent_chance``,
  pushing the interrupted scene on a resume stack and branching into a bounded,
  composable, off-goal sub-graph that ALWAYS ends in a RETURN edge. Tangents are
  bounded by ``max_tangents`` / ``max_depth`` and are recorded inline (``tangent``
  + ``resumes_after``) so the plan is auditable.

All entropy is drawn from one RNG seeded from ``session_seed`` (reproducible) or
fresh OS entropy (genuinely different per attempt), mirroring the motor layer.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Any

from imposter5.automation_connector.humanize_dist import lognormal_ms
from imposter5.story.task_intent import GoalPredicate, TaskIntent

# Per-scene base dwell (ms) and the affordance ROLE the scene primarily acts on.
_SCENE_BASE_DWELL_MS: dict[str, float] = {
    "search_open": 700.0,
    "search_query": 1800.0,
    "results_scan": 2600.0,
    "profile_open": 650.0,
    "profile_read": 3200.0,
    "profile_back": 520.0,
    "tangent_open_profile": 720.0,
    "tangent_read": 2800.0,
    "tangent_back": 520.0,
    "tangent_research": 2000.0,
    "tangent_refresh": 1200.0,
}
_SCENE_TARGET_ROLE: dict[str, str | None] = {
    "search_open": "search_input",
    "search_query": "search_input",
    "results_scan": "result_list",
    "profile_open": "result_open",
    "profile_read": "profile_section",
    "profile_back": "back_control",
    "tangent_open_profile": "result_open",
    "tangent_read": "profile_section",
    "tangent_back": "back_control",
    "tangent_research": "search_input",
    "tangent_refresh": "back_control",
}

# Main scenes at which a human plausibly gets curious mid-task.
_TANGENT_ELIGIBLE = frozenset({"results_scan", "profile_open", "profile_read"})

# Dwell clamps so the log-normal tail stays plausible.
_DWELL_LO_MS = 150
_DWELL_HI_MS = 20_000


@dataclass(frozen=True)
class Scene:
    """One concrete step in a sampled StoryPlan."""

    name: str
    kind: str  # "main" | "tangent"
    dwell_ms: int
    target_role: str | None = None
    depth: int = 0
    tangent: bool = False
    is_return: bool = False
    resumes_after: str | None = None
    note: str = ""

    def to_payload(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "dwell_ms": self.dwell_ms,
            "target_role": self.target_role,
            "depth": self.depth,
        }
        if self.tangent:
            d["tangent"] = True
            d["resumes_after"] = self.resumes_after
            if self.is_return:
                d["is_return"] = True
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class StoryPlan:
    scenes: list[Scene]
    query_hint: str
    goal_predicate: GoalPredicate
    seed: Any = None
    seed_source: str = "entropy"
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def main_scenes(self) -> list[Scene]:
        return [s for s in self.scenes if not s.tangent]

    @property
    def tangent_count(self) -> int:
        # Each tangent EXCURSION ends in exactly one RETURN edge, so counting returns
        # counts excursions (including nested ones).
        return sum(1 for s in self.scenes if s.tangent and s.is_return)

    def signature(self) -> tuple:
        """Structural signature (ignores exact dwell) for variety checks."""
        return tuple((s.name, s.kind, s.depth, s.is_return) for s in self.scenes)

    def to_payload(self) -> dict[str, Any]:
        return {
            "query_hint": self.query_hint,
            "goal_predicate": {
                "type": self.goal_predicate.type,
                "target": self.goal_predicate.target,
                "jitter": self.goal_predicate.jitter,
            },
            "seed_source": self.seed_source,
            "scene_count": len(self.scenes),
            "tangent_count": self.tangent_count,
            "scenes": [s.to_payload() for s in self.scenes],
            "meta": dict(self.meta),
        }


class StoryCompiler:
    def __init__(self, intent: TaskIntent) -> None:
        self.intent = intent

    # --- entropy ------------------------------------------------------------------
    @staticmethod
    def _make_rng(seed: Any) -> tuple[random.Random, str]:
        if seed is not None and str(seed) != "":
            return random.Random(str(seed)), "seed"
        return random.Random(os.urandom(16)), "entropy"

    def _dwell(self, rng: random.Random, scene: str) -> int:
        base = _SCENE_BASE_DWELL_MS.get(scene, 1200.0)
        cv = max(0.05, float(self.intent.variance.dwell_cv))
        return int(lognormal_ms(rng, mean_ms=base, cv=cv, lo=_DWELL_LO_MS, hi=_DWELL_HI_MS))

    def _main(self, rng: random.Random, name: str, note: str = "") -> Scene:
        return Scene(
            name=name,
            kind="main",
            dwell_ms=self._dwell(rng, name),
            target_role=_SCENE_TARGET_ROLE.get(name),
            depth=0,
            note=note,
        )

    # --- backbone -----------------------------------------------------------------
    def _required_scenes(self) -> set[str]:
        """Scenes the goal predicate REQUIRES, regardless of what the intent listed.

        Guarantees goal reachability even if the prompt omitted a needed scene.
        """
        ptype = self.intent.goal_predicate.type
        if ptype == "scan_fraction":
            return {"search_open", "search_query", "results_scan"}
        if ptype == "open_count":
            return {"search_open", "search_query", "results_scan", "profile_open"}
        if ptype == "find_in_profile":
            return {"search_open", "search_query", "results_scan", "profile_open", "profile_read"}
        return set()

    def _backbone(self, rng: random.Random) -> list[Scene]:
        present = set(self.intent.objective.main_scenes) | self._required_scenes()
        var = self.intent.variance

        scenes: list[Scene] = []
        if "search_open" in present:
            scenes.append(self._main(rng, "search_open"))
        if "search_query" in present:
            scenes.append(self._main(rng, "search_query"))

        # Scan units and profile cycles are the body. Counts vary (partial
        # substitution); their interleave varies (order jitter).
        n_scan = rng.randint(1, 3) if "results_scan" in present else 0
        if n_scan == 0:
            n_scan = 1  # always at least one scan so a result set is established
        can_profile = "profile_open" in present
        n_cycles = rng.randint(1, 3) if can_profile else 0
        if not var.partial_substitution:
            # Deterministic body when substitution is disabled: one scan + one cycle.
            n_scan = 1
            n_cycles = 1 if can_profile else 0

        scan_units = ["results_scan"] * n_scan
        cycle_units = list(range(n_cycles))

        # Build the body: at least one scan must precede the first profile cycle so
        # there is a result set to open from (goal reachability + realism).
        body: list[tuple[str, Any]] = [("scan", None)]
        remaining: list[tuple[str, Any]] = [("scan", None)] * (len(scan_units) - 1) + [
            ("cycle", c) for c in cycle_units
        ]
        if var.order_jitter:
            rng.shuffle(remaining)
        else:
            remaining.sort(key=lambda u: 0 if u[0] == "scan" else 1)
        body.extend(remaining)

        for kind, _payload in body:
            if kind == "scan":
                scenes.append(self._main(rng, "results_scan"))
            else:
                scenes.extend(self._profile_cycle(rng, present))
        return scenes

    def _profile_cycle(self, rng: random.Random, present: set[str]) -> list[Scene]:
        out = [self._main(rng, "profile_open")]
        var = self.intent.variance
        include_read = "profile_read" in present and (
            not var.partial_substitution or rng.random() < 0.8
        )
        include_back = "profile_back" in present or include_read
        if include_read:
            out.append(self._main(rng, "profile_read"))
        elif "profile_read" in present:
            out.append(self._main(rng, "profile_open", note="glance_substituted_for_read"))
        if include_back and ("profile_back" in present or var.partial_substitution):
            out.append(self._main(rng, "profile_back"))
        return out

    # --- curiosity tangents -------------------------------------------------------
    def _tangent_dwell(self, rng: random.Random, name: str) -> int:
        return self._dwell(rng, name)

    def _build_tangent(
        self,
        rng: random.Random,
        *,
        resumes_after: str,
        depth: int,
        budget: dict[str, int],
    ) -> list[Scene]:
        """Build ONE bounded tangent excursion that ALWAYS returns.

        Composable (may nest up to ``max_depth``) and off-goal by construction. The
        excursion always ends in a RETURN edge (``is_return=True``) that pops back to
        ``resumes_after``.
        """
        cur = self.intent.curiosity
        avail = set(cur.tangent_scenes)
        budget["left"] -= 1

        def mk(name: str, *, is_return: bool = False, note: str = "") -> Scene:
            return Scene(
                name=name,
                kind="tangent",
                dwell_ms=self._tangent_dwell(rng, name),
                target_role=_SCENE_TARGET_ROLE.get(name),
                depth=depth,
                tangent=True,
                is_return=is_return,
                resumes_after=resumes_after,
                note=note,
            )

        scenes: list[Scene] = []

        # Choose an entry among the available tangent kinds (excluding the return-only
        # primitives). tangent_refresh is itself a self-returning excursion.
        entries = [s for s in ("tangent_open_profile", "tangent_research", "tangent_refresh") if s in avail]
        if not entries:
            # Only a read/back vocabulary was given: a minimal open->back excursion.
            entries = ["tangent_open_profile"]
        entry = rng.choice(entries)

        if entry == "tangent_refresh":
            # back + reload: a single self-returning excursion.
            scenes.append(mk("tangent_refresh", is_return=True, note="back_and_reload"))
            return scenes

        if entry == "tangent_research":
            scenes.append(mk("tangent_research", note="off_goal_query"))
            if "tangent_read" in avail and rng.random() < 0.5:
                scenes.append(mk("tangent_read"))
            # Nested curiosity within the research excursion.
            scenes.extend(self._maybe_nested(rng, resumes_after, depth, budget))
            scenes.append(mk("tangent_back", is_return=True, note="restore_original_query"))
            return scenes

        # entry == tangent_open_profile (default): open a NON-objective item, read, back.
        scenes.append(mk("tangent_open_profile", note="off_goal_profile"))
        if "tangent_read" in avail:
            scenes.append(mk("tangent_read"))
            scenes.extend(self._maybe_nested(rng, resumes_after, depth, budget))
        scenes.append(mk("tangent_back", is_return=True))
        return scenes

    def _maybe_nested(
        self, rng: random.Random, resumes_after: str, depth: int, budget: dict[str, int]
    ) -> list[Scene]:
        cur = self.intent.curiosity
        if depth >= cur.max_depth or budget["left"] <= 0:
            return []
        if rng.random() >= cur.tangent_chance:
            return []
        # Nested excursion resumes after the parent tangent's read.
        return self._build_tangent(
            rng, resumes_after=resumes_after, depth=depth + 1, budget=budget
        )

    # --- compile ------------------------------------------------------------------
    def compile(self, *, seed: Any = None) -> StoryPlan:
        rng, source = self._make_rng(seed)
        backbone = self._backbone(rng)
        cur = self.intent.curiosity
        budget = {"left": int(cur.max_tangents)}

        scenes: list[Scene] = []
        for scene in backbone:
            scenes.append(scene)
            if (
                cur.enabled
                and budget["left"] > 0
                and scene.name in _TANGENT_ELIGIBLE
                and rng.random() < cur.tangent_chance
            ):
                scenes.extend(
                    self._build_tangent(
                        rng, resumes_after=scene.name, depth=1, budget=budget
                    )
                )

        meta = {
            "site": self.intent.site,
            "archetype": self.intent.archetype,
            "cadence": self.intent.cadence,
            "order_jitter": self.intent.variance.order_jitter,
            "partial_substitution": self.intent.variance.partial_substitution,
            "dwell_cv": self.intent.variance.dwell_cv,
            "tangents_requested_max": cur.max_tangents,
            "max_depth": cur.max_depth,
        }
        return StoryPlan(
            scenes=scenes,
            query_hint=self.intent.query_hint,
            goal_predicate=self.intent.goal_predicate,
            seed=seed,
            seed_source=source,
            meta=meta,
        )


def compile_story(intent: TaskIntent, *, seed: Any = None) -> StoryPlan:
    """Convenience wrapper: sample one StoryPlan from a TaskIntent."""
    return StoryCompiler(intent).compile(seed=seed)
