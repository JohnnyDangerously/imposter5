"""TaskIntent (``lhhl-task-intent/v1``): the small prompt that seeds a plan.

A TaskIntent is a neutral, site-agnostic description of a goal-oriented session:
the objective (an ordered list of *scenes* plus a goal predicate), a curiosity
budget (how often/deep the agent may wander off-goal), variance knobs, and a
human cadence hint. It is parsed and validated here; nothing in this module
touches a browser or a DOM. Validation is strict: a malformed intent raises
:class:`TaskIntentError` rather than silently degrading to a fabricated default
(see the repo "no fallbacks" rule), but every *optional* block has an honest,
documented default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any

SCHEMA = "lhhl-task-intent/v1"

# Scenes the compiler/executor understand. These are SEMANTIC scene names, not site
# ids: the SiteMapper maps each scene's target to a generic affordance ROLE at run
# time, so the same intent works on any conformant site.
MAIN_SCENE_VOCAB = frozenset(
    {
        "search_open",
        "search_query",
        "results_scan",
        "profile_open",
        "profile_read",
        "profile_back",
    }
)
TANGENT_SCENE_VOCAB = frozenset(
    {
        "tangent_open_profile",
        "tangent_read",
        "tangent_back",
        "tangent_research",
        "tangent_refresh",
    }
)

GOAL_PREDICATE_TYPES = frozenset({"scan_fraction", "open_count", "find_in_profile"})


class TaskIntentError(ValueError):
    """Raised when a TaskIntent payload is structurally invalid."""


@dataclass(frozen=True)
class GoalPredicate:
    """When the session is "done enough".

    - ``scan_fraction``: fraction of the result set the operator scanned; ``target``
      in [0, 1] with ``jitter`` modelling human over/undershoot.
    - ``open_count``: number of (objective) profiles opened; ``target`` is a count,
      ``jitter`` is an integer-ish slack applied as a fraction of target.
    - ``find_in_profile``: open profiles until one is "read" (a section dwell on an
      opened profile); ``target`` is the count of satisfied reads.
    """

    type: str
    target: float
    jitter: float = 0.0

    def __post_init__(self) -> None:
        if self.type not in GOAL_PREDICATE_TYPES:
            raise TaskIntentError(
                f"goal_predicate.type {self.type!r} not in {sorted(GOAL_PREDICATE_TYPES)}"
            )
        if self.type == "scan_fraction" and not (0.0 <= float(self.target) <= 1.0):
            raise TaskIntentError("scan_fraction target must be in [0, 1]")
        if float(self.target) < 0:
            raise TaskIntentError("goal_predicate.target must be non-negative")
        if float(self.jitter) < 0:
            raise TaskIntentError("goal_predicate.jitter must be non-negative")


@dataclass(frozen=True)
class Objective:
    main_scenes: tuple[str, ...]
    query_hint: str
    goal_predicate: GoalPredicate

    def __post_init__(self) -> None:
        if not self.main_scenes:
            raise TaskIntentError("objective.main_scenes must be non-empty")
        unknown = [s for s in self.main_scenes if s not in MAIN_SCENE_VOCAB]
        if unknown:
            raise TaskIntentError(
                f"objective.main_scenes has unknown scene(s) {unknown}; "
                f"valid: {sorted(MAIN_SCENE_VOCAB)}"
            )


@dataclass(frozen=True)
class Curiosity:
    """Intent-level wander budget (distinct from motor noise).

    ``tangent_chance`` is the per-eligible-scene probability of branching onto a
    tangent; ``max_tangents`` and ``max_depth`` bound how many and how deep tangents
    may go so the agent always returns and completes the main goal.
    """

    tangent_chance: float = 0.0
    max_tangents: int = 0
    max_depth: int = 1
    tangent_scenes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not (0.0 <= float(self.tangent_chance) <= 1.0):
            raise TaskIntentError("curiosity.tangent_chance must be in [0, 1]")
        if int(self.max_tangents) < 0:
            raise TaskIntentError("curiosity.max_tangents must be >= 0")
        if int(self.max_depth) < 0:
            raise TaskIntentError("curiosity.max_depth must be >= 0")
        unknown = [s for s in self.tangent_scenes if s not in TANGENT_SCENE_VOCAB]
        if unknown:
            raise TaskIntentError(
                f"curiosity.tangent_scenes has unknown scene(s) {unknown}; "
                f"valid: {sorted(TANGENT_SCENE_VOCAB)}"
            )

    @property
    def enabled(self) -> bool:
        return self.tangent_chance > 0.0 and self.max_tangents > 0 and bool(self.tangent_scenes)


@dataclass(frozen=True)
class Variance:
    """Plan-time randomization knobs (what makes no two attempts alike)."""

    dwell_cv: float = 0.35
    order_jitter: bool = True
    partial_substitution: bool = True

    def __post_init__(self) -> None:
        if not (0.0 <= float(self.dwell_cv) <= 2.0):
            raise TaskIntentError("variance.dwell_cv must be in [0, 2]")


@dataclass(frozen=True)
class TaskIntent:
    site: str
    archetype: str
    describe: str
    objective: Objective
    curiosity: Curiosity = field(default_factory=Curiosity)
    variance: Variance = field(default_factory=Variance)
    cadence: str = ""
    schema: str = SCHEMA

    @property
    def query_hint(self) -> str:
        return self.objective.query_hint

    @property
    def goal_predicate(self) -> GoalPredicate:
        return self.objective.goal_predicate


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskIntentError(f"{name} must be an object, got {type(value).__name__}")
    return value


def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TaskIntentError(f"{name} must be a list of strings")
    return tuple(str(v) for v in value)


def parse_task_intent(data: dict[str, Any]) -> TaskIntent:
    """Parse + strictly validate a TaskIntent payload into typed dataclasses."""
    data = _require_dict(data, "task_intent")

    schema = str(data.get("schema") or SCHEMA)
    if schema != SCHEMA:
        raise TaskIntentError(f"unsupported schema {schema!r}; expected {SCHEMA!r}")

    obj_raw = _require_dict(data.get("objective"), "objective")
    pred_raw = _require_dict(obj_raw.get("goal_predicate"), "objective.goal_predicate")
    predicate = GoalPredicate(
        type=str(pred_raw.get("type", "")),
        target=float(pred_raw.get("target", 0.0)),
        jitter=float(pred_raw.get("jitter", 0.0)),
    )
    objective = Objective(
        main_scenes=_str_tuple(obj_raw.get("main_scenes"), "objective.main_scenes"),
        query_hint=str(obj_raw.get("query_hint", "")),
        goal_predicate=predicate,
    )

    cur_raw = data.get("curiosity")
    if cur_raw is None:
        curiosity = Curiosity()
    else:
        cur_raw = _require_dict(cur_raw, "curiosity")
        curiosity = Curiosity(
            tangent_chance=float(cur_raw.get("tangent_chance", 0.0)),
            max_tangents=int(cur_raw.get("max_tangents", 0)),
            max_depth=int(cur_raw.get("max_depth", 1)),
            tangent_scenes=_str_tuple(cur_raw.get("tangent_scenes"), "curiosity.tangent_scenes"),
        )

    var_raw = data.get("variance")
    if var_raw is None:
        variance = Variance()
    else:
        var_raw = _require_dict(var_raw, "variance")
        variance = Variance(
            dwell_cv=float(var_raw.get("dwell_cv", 0.35)),
            order_jitter=bool(var_raw.get("order_jitter", True)),
            partial_substitution=bool(var_raw.get("partial_substitution", True)),
        )

    return TaskIntent(
        site=str(data.get("site", "")),
        archetype=str(data.get("archetype", "")),
        describe=str(data.get("describe", "")),
        objective=objective,
        curiosity=curiosity,
        variance=variance,
        cadence=str(data.get("cadence", "")),
        schema=schema,
    )


def load_task_intent(path_or_json: str) -> TaskIntent:
    """Load a TaskIntent from a file path OR an inline JSON string."""
    text = path_or_json
    if os.path.exists(path_or_json):
        with open(path_or_json, "r", encoding="utf-8") as f:
            text = f.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskIntentError(f"task_intent is not valid JSON: {exc}") from exc
    return parse_task_intent(data)
