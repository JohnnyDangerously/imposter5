"""Bounded behavior policy for automation connector browser runs.

This module models pacing, partial completion, and input ergonomics for one
observation run.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import random
import secrets
from typing import Any

from imposter5.automation_connector.goals import GoalSpec, goal_spec_to_payload

POLICY_VERSION = "goal-behavior-v1"


@dataclass(frozen=True)
class Persona:
    name: str
    patience: str
    scroll_style: str
    dwell_multiplier: float
    scroll_multiplier: float
    interaction_style: str = "low_touch"


@dataclass(frozen=True)
class CompletionLevel:
    name: str
    weight: int
    max_scroll_passes: int
    collect_visible_state: bool = True


PERSONAS = (
    Persona("focused_power_user", "medium", "direct_scan", 0.82, 1.10, "low_touch"),
    Persona("curious_reader", "high", "pause_and_read", 1.25, 0.82, "inspect_then_move"),
    Persona("impatient_scanner", "low", "long_skim", 0.68, 1.25, "minimal"),
    Persona("slow_reader", "high", "short_partial_scrolls", 1.55, 0.72, "inspect_then_move"),
    Persona("methodical_operator", "medium", "section_scan", 1.05, 0.95, "confirm_before_click"),
    Persona("mobile_checker", "medium", "short_swipes", 1.15, 0.70, "touch_first"),
    Persona("late_day_review", "high", "pause_and_read", 1.35, 0.78, "low_touch"),
)

COMPLETION_LADDER = (
    CompletionLevel("glance_only", 15, 1),
    CompletionLevel("skim_visible_feed", 35, 2),
    CompletionLevel("review_feed", 35, 3),
    CompletionLevel("deep_review_feed", 15, 4),
)

DEFAULT_WAIT_MS = 1_500
DEFAULT_SCROLL_DELTA_Y = 900
MAX_SCROLL_PASSES = 4


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _record_sessions_enabled() -> bool:
    """Session recording is opt-in (dev/test) and off by default."""
    return os.environ.get("AUTOMATION_CONNECTOR_RECORD_SESSIONS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _bounded_int(value: Any, *, lower: int, upper: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _bounded_float(value: Any, *, lower: float, upper: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _bounded_ms(value: float, *, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(round(value))))


def _completion_from_raw(raw: Any) -> CompletionLevel | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    weight = _bounded_int(raw.get("weight", raw.get("percent", 1)), lower=1, upper=100, default=1)
    scrolls = _bounded_int(
        raw.get("max_scroll_passes", raw.get("scroll_passes", 1)),
        lower=0,
        upper=MAX_SCROLL_PASSES,
        default=1,
    )
    return CompletionLevel(
        name=name,
        weight=weight,
        max_scroll_passes=scrolls,
        collect_visible_state=bool(raw.get("collect_visible_state", True)),
    )


def _configured_completion_ladder(target: dict[str, Any]) -> tuple[CompletionLevel, ...]:
    raw_ladder: Any = target.get("completion_ladder")
    if raw_ladder is None:
        raw_env = os.environ.get("AUTOMATION_CONNECTOR_COMPLETION_LADDER_JSON")
        if raw_env:
            try:
                raw_ladder = json.loads(raw_env)
            except json.JSONDecodeError:
                raw_ladder = None
    if isinstance(raw_ladder, str):
        try:
            raw_ladder = json.loads(raw_ladder)
        except json.JSONDecodeError:
            raw_ladder = None
    if not isinstance(raw_ladder, list):
        return COMPLETION_LADDER
    ladder = tuple(level for level in (_completion_from_raw(item) for item in raw_ladder) if level)
    return ladder or COMPLETION_LADDER


def _choose_completion(rng: random.Random, ladder: tuple[CompletionLevel, ...]) -> CompletionLevel:
    return rng.choices(ladder, weights=[level.weight for level in ladder], k=1)[0]


def _goal_payload(goal: Any) -> dict[str, Any]:
    if isinstance(goal, GoalSpec):
        return goal_spec_to_payload(goal)
    if isinstance(goal, dict):
        return goal
    return {"name": str(goal or "observe_visible_page_state")}


def _goal_name(goal_payload: dict[str, Any]) -> str:
    return str(goal_payload.get("name") or "observe_visible_page_state")


def _bool_from_target(target: dict[str, Any], name: str, default: bool = False) -> bool:
    raw = target.get(name, default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "mobile"}
    return bool(raw)


def build_behavior_plan(
    target: dict[str, Any],
    *,
    provider: str,
    goal: Any = "observe_visible_page_state",
    seed: str | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe pacing and interaction plan for one automation run."""
    run_id = secrets.token_hex(8)
    rng = random.Random(seed or f"{run_id}:{target.get('id')}:{provider}")
    persona = rng.choice(PERSONAS)
    ladder = _configured_completion_ladder(target)
    completion = _choose_completion(rng, ladder)
    goal_payload = _goal_payload(goal)
    device_mode = str(target.get("automation_device") or target.get("device_mode") or "desktop").strip().lower()
    mobile_enabled = device_mode == "mobile" or _bool_from_target(target, "mobile_gestures", False)

    base_wait = _env_int("AUTOMATION_CONNECTOR_WAIT_MS", DEFAULT_WAIT_MS)
    base_scroll = _env_int("AUTOMATION_CONNECTOR_SCROLL_DELTA_Y", DEFAULT_SCROLL_DELTA_Y)
    scroll_passes = min(completion.max_scroll_passes, MAX_SCROLL_PASSES)
    waits = [
        _bounded_ms(base_wait * persona.dwell_multiplier * rng.uniform(0.55, 1.75), lower=250, upper=12_000)
        for _ in range(max(1, scroll_passes))
    ]
    scroll_deltas = [
        _bounded_ms(base_scroll * persona.scroll_multiplier * rng.uniform(0.5, 1.6), lower=120, upper=2_000)
        for _ in range(max(0, scroll_passes - 1))
    ]

    return {
        "policy_version": POLICY_VERSION,
        "run_id": run_id,
        "provider": provider,
        "persona": {
            "name": persona.name,
            "patience": persona.patience,
            "scroll_style": persona.scroll_style,
            "interaction_style": persona.interaction_style,
        },
        "completion": {
            "name": completion.name,
            "max_scroll_passes": scroll_passes,
            "collect_visible_state": completion.collect_visible_state,
            "ladder": [
                {
                    "name": level.name,
                    "weight": level.weight,
                    "max_scroll_passes": level.max_scroll_passes,
                }
                for level in ladder
            ],
        },
        "goal_spec": goal_payload,
        "pacing": {
            "wait_ms": waits,
            "scroll_delta_y": scroll_deltas,
        },
        "typing": {
            "min_delay_ms": _bounded_int(target.get("typing_min_delay_ms"), lower=20, upper=400, default=55),
            "max_delay_ms": _bounded_int(target.get("typing_max_delay_ms"), lower=45, upper=800, default=170),
            "typo_chance": _bounded_float(target.get("typing_typo_chance"), lower=0.0, upper=0.08, default=0.015),
            "correction_chance": _bounded_float(
                target.get("typing_correction_chance"),
                lower=0.0,
                upper=1.0,
                default=0.92,
            ),
            "pause_mid_query_chance": _bounded_float(
                target.get("typing_pause_mid_query_chance"),
                lower=0.0,
                upper=0.35,
                default=0.08,
            ),
        },
        "pointer": {
            "move_style": rng.choice(("direct", "slight_arc", "two_step")),
            "hover_before_click_chance": _bounded_float(
                target.get("hover_before_click_chance"),
                lower=0.0,
                upper=0.6,
                default=0.18,
            ),
            "imprecision_px": _bounded_int(target.get("pointer_imprecision_px"), lower=0, upper=10, default=3),
            "overshoot_chance": _bounded_float(target.get("pointer_overshoot_chance"), lower=0.0, upper=0.2, default=0.04),
        },
        "hover": {
            "expand_comments_chance": _bounded_float(
                target.get("expand_comments_chance"),
                lower=0.0,
                upper=0.35,
                default=0.04,
            ),
            "max_expansions": _bounded_int(target.get("max_comment_expansions"), lower=0, upper=3, default=1),
            "hover_dwell_ms": _bounded_int(target.get("hover_dwell_ms"), lower=150, upper=1_500, default=450),
        },
        "backtracking": {
            "micro_abandon_chance": _bounded_float(
                target.get("micro_abandon_chance"),
                lower=0.0,
                upper=0.25,
                default=0.03,
            ),
            "max_backtracks": _bounded_int(target.get("max_backtracks"), lower=0, upper=2, default=1),
        },
        "mobile": {
            "enabled": mobile_enabled,
            "gesture_style": "short_swipe" if mobile_enabled else "none",
            "max_swipes": _bounded_int(target.get("max_mobile_swipes"), lower=0, upper=4, default=2 if mobile_enabled else 0),
        },
        "recorder": {
            "enabled": _record_sessions_enabled(),
            "max_events": _bounded_int(target.get("session_recorder_max_events"), lower=20, upper=500, default=160),
        },
        "analytics": {
            "synthetic": True,
            "labels": [
                "automation_connector",
                f"provider:{provider}",
                f"goal:{_goal_name(goal_payload)}",
                f"completion:{completion.name}",
                f"device:{'mobile' if mobile_enabled else 'desktop'}",
            ],
        },
        # Variation guides (custom or auto) for activity mix / micro-behaviors.
        # Especially enriches the static LinkedIn observation path (and any future specialized providers)
        # with the described human-like feed reading, bidirectional scrolls, hovers, comment expands,
        # profile peeks (click name -> scroll work history -> back), notifications checks, etc.
        # These are *not* prompt-interpreted actions (see goal_runner for the full agent prompt path);
        # they are bounded, randomized, persona+completion-driven variations on the fixed observation goal
        # so that even high-volume static runs (LinkedIn experiment) produce varied, mouse-involved traces.
        # Provide via target["variation_guide"] (or linkedin_variation_guide) when creating target for custom control.
        **_build_variations_section(target, provider, persona, completion, rng),
    }


def behavior_summary(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Return the store-safe, compact subset of a behavior plan."""
    if not isinstance(plan, dict) or not plan.get("run_id"):
        return {}
    completion = plan.get("completion") if isinstance(plan.get("completion"), dict) else {}
    persona = plan.get("persona") if isinstance(plan.get("persona"), dict) else {}
    goal_spec = plan.get("goal_spec") if isinstance(plan.get("goal_spec"), dict) else {}
    analytics = plan.get("analytics") if isinstance(plan.get("analytics"), dict) else {}
    mobile = plan.get("mobile") if isinstance(plan.get("mobile"), dict) else {}
    return {
        "policy_version": plan.get("policy_version"),
        "run_id": plan.get("run_id"),
        "provider": plan.get("provider"),
        "goal": goal_spec.get("name"),
        "persona": persona.get("name"),
        "completion": completion.get("name"),
        "max_scroll_passes": completion.get("max_scroll_passes"),
        "device": "mobile" if mobile.get("enabled") else "desktop",
        "analytics_labels": analytics.get("labels") if isinstance(analytics.get("labels"), list) else [],
        "variations": plan.get("variations") or {},
    }


def planned_wait_ms(plan: dict[str, Any] | None, pass_index: int, fallback: int) -> int:
    pacing = plan.get("pacing") if isinstance(plan, dict) else None
    waits = pacing.get("wait_ms") if isinstance(pacing, dict) else None
    if isinstance(waits, list) and waits:
        return _bounded_int(waits[min(pass_index, len(waits) - 1)], lower=100, upper=10_000, default=fallback)
    return fallback


def planned_scroll_delta(plan: dict[str, Any] | None, pass_index: int, fallback: int) -> int:
    pacing = plan.get("pacing") if isinstance(plan, dict) else None
    deltas = pacing.get("scroll_delta_y") if isinstance(pacing, dict) else None
    if isinstance(deltas, list) and deltas:
        return _bounded_int(deltas[min(pass_index, len(deltas) - 1)], lower=50, upper=2_000, default=fallback)
    return fallback


def planned_scroll_passes(plan: dict[str, Any] | None, fallback: int) -> int:
    completion = plan.get("completion") if isinstance(plan, dict) else None
    if isinstance(completion, dict):
        return _bounded_int(
            completion.get("max_scroll_passes"),
            lower=1,
            upper=MAX_SCROLL_PASSES,
            default=fallback,
        )
    return fallback


def _build_variations_section(
    target: dict[str, Any],
    provider: str,
    persona: "Persona",
    completion: "CompletionLevel",
    rng: random.Random,
) -> dict[str, Any]:
    """Compute the 'variations' and 'variation_chances' blocks for the plan.

    Supports custom via target['variation_guide'] (or 'linkedin_variation_guide').
    Auto-derives sensible defaults from persona + completion for the LinkedIn static path
    (and others) so runs do the human micro-variations the user described: varied mouse-positioned
    bidirectional feed scrolls, post hovers, comment expands, occasional profile peeks (name/picture click,
    scroll to work history, back), notifications checks, without full prompt interpretation.
    """
    guide: dict[str, Any] = {}
    raw_guide = target.get("variation_guide") or target.get("linkedin_variation_guide") or target.get("activity_variations")
    if isinstance(raw_guide, dict):
        guide = raw_guide
    elif isinstance(raw_guide, (list, tuple)):
        guide = {"activities": list(raw_guide)}

    is_linkedin = provider in ("linkedin", "linkedin_profile", "linkedin_company") or "linkedin" in str(target.get("entity_type", "")).lower()

    # Base from persona/completion
    base_max_side = min(4, max(0, completion.max_scroll_passes - 1)) if is_linkedin else 0
    max_side = _bounded_int(guide.get("max_side_actions") or guide.get("max_variations"), lower=0, upper=6, default=base_max_side)

    variations: dict[str, Any] = {
        "bidirectional_scroll": bool(guide.get("bidirectional_scroll", persona.scroll_style in ("pause_and_read", "short_partial_scrolls", "section_scan"))),
        "hover_and_read": bool(guide.get("hover_and_read", persona.interaction_style in ("inspect_then_move", "confirm_before_click", "low_touch"))),
        "expand_comments": bool(guide.get("expand_comments", True)),
        "profile_peeks": bool(guide.get("profile_peeks", is_linkedin and completion.max_scroll_passes >= 2)),
        "notifications_check": bool(guide.get("notifications_check", is_linkedin)),
        "avatar_or_picture_clicks": bool(guide.get("avatar_or_picture_clicks", is_linkedin and rng.random() < 0.15)),
        "max_side_actions": max_side,
        "is_linkedin": is_linkedin,
        "source": "custom" if guide else "auto",
    }
    if "activities" in guide and isinstance(guide["activities"], (list, tuple)):
        allowed = {str(a).strip().lower() for a in guide["activities"]}
        for k in list(variations.keys()):
            if k in ("max_side_actions", "is_linkedin", "source"):
                continue
            variations[k] = variations[k] and (k.replace("_", "") in allowed or k in allowed)

    chances: dict[str, Any] = {
        "profile_peek": _bounded_float(
            guide.get("profile_peek_chance", 0.20 if variations["profile_peeks"] else 0.0),
            lower=0.0,
            upper=0.6,
            default=0.0,
        ),
        "notifications": _bounded_float(
            guide.get("notifications_chance", 0.12 if variations["notifications_check"] else 0.0),
            lower=0.0,
            upper=0.5,
            default=0.0,
        ),
        "comment_expand": _bounded_float(
            guide.get("comment_expand_chance", 0.10),
            lower=0.0,
            upper=0.35,
            default=0.0,
        ),
        "bidir_scroll": 0.28 if variations["bidirectional_scroll"] else 0.06,
        "hover_read": 0.55 if variations["hover_and_read"] else 0.15,
    }
    return {"variations": variations, "variation_chances": chances}
