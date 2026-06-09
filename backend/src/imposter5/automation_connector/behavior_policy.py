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
from imposter5.automation_connector.humanize_dist import lognormal_ms

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


PERSONAS_FILE_PATH = os.path.join(os.path.dirname(__file__), "personas.json")

DEFAULT_PERSONAS = [
    {"name": "focused_power_user", "patience": "medium", "scroll_style": "direct_scan", "dwell_multiplier": 0.82, "scroll_multiplier": 1.10, "interaction_style": "low_touch"},
    {"name": "curious_reader", "patience": "high", "scroll_style": "pause_and_read", "dwell_multiplier": 1.25, "scroll_multiplier": 0.82, "interaction_style": "inspect_then_move"},
    {"name": "impatient_scanner", "patience": "low", "scroll_style": "long_skim", "dwell_multiplier": 0.68, "scroll_multiplier": 1.25, "interaction_style": "minimal"},
    {"name": "slow_reader", "patience": "high", "scroll_style": "short_partial_scrolls", "dwell_multiplier": 1.55, "scroll_multiplier": 0.72, "interaction_style": "inspect_then_move"},
    {"name": "methodical_operator", "patience": "medium", "scroll_style": "section_scan", "dwell_multiplier": 1.05, "scroll_multiplier": 0.95, "interaction_style": "confirm_before_click"},
    {"name": "mobile_checker", "patience": "medium", "scroll_style": "short_swipes", "dwell_multiplier": 1.15, "scroll_multiplier": 0.70, "interaction_style": "touch_first"},
    {"name": "late_day_review", "patience": "high", "scroll_style": "pause_and_read", "dwell_multiplier": 1.35, "scroll_multiplier": 0.78, "interaction_style": "low_touch"},
]


def load_personas() -> list[Persona]:
    try:
        if os.path.exists(PERSONAS_FILE_PATH):
            with open(PERSONAS_FILE_PATH, "r") as f:
                data = json.load(f)
                return [
                    Persona(
                        name=p["name"],
                        patience=p["patience"],
                        scroll_style=p["scroll_style"],
                        dwell_multiplier=float(p["dwell_multiplier"]),
                        scroll_multiplier=float(p["scroll_multiplier"]),
                        interaction_style=p.get("interaction_style", "low_touch")
                    )
                    for p in data
                ]
    except Exception:
        pass

    try:
        with open(PERSONAS_FILE_PATH, "w") as f:
            json.dump(DEFAULT_PERSONAS, f, indent=2)
    except Exception:
        pass

    return [
        Persona(
            name=p["name"],
            patience=p["patience"],
            scroll_style=p["scroll_style"],
            dwell_multiplier=p["dwell_multiplier"],
            scroll_multiplier=p["scroll_multiplier"],
            interaction_style=p["interaction_style"]
        )
        for p in DEFAULT_PERSONAS
    ]


def save_personas(personas_list: list[dict[str, Any]]) -> None:
    global PERSONAS
    try:
        with open(PERSONAS_FILE_PATH, "w") as f:
            json.dump(personas_list, f, indent=2)
    except Exception:
        pass
    try:
        loaded = load_personas()
        PERSONAS.clear()
        PERSONAS.extend(loaded)
    except Exception:
        pass


PERSONAS = load_personas()

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


def _stable_noisy(
    identity_rng: random.Random,
    session_rng: random.Random,
    *,
    lo: float,
    hi: float,
    session_jitter: float,
) -> float:
    """Draw a parameter that is STABLE per identity yet NEVER identical per session.

    The identity owns a baseline drawn once from ``identity_rng`` (so the same
    identity reproduces the same baseline across all its sessions). Each session then
    perturbs that baseline by a small multiplicative noise from ``session_rng``
    (``±session_jitter``). This is the empirical signature of a real person: motor
    and timing parameters are self-consistent across days but exhibit trial-to-trial
    variability driven by signal-dependent neuromotor noise (Harris & Wolpert, 1998,
    *Nature*). It is also what defeats long-term cross-session clustering: two
    sessions of one identity are self-similar but distinct, while two identities draw
    different baselines and are clearly separated.
    """
    baseline = identity_rng.uniform(lo, hi)
    jit = max(0.0, float(session_jitter))
    value = baseline * (1.0 + session_rng.uniform(-jit, jit))
    return max(lo, min(hi, value))


def build_identity_kinematics(
    identity_rng: random.Random,
    session_rng: random.Random,
) -> dict[str, Any]:
    """Build a per-identity, per-session kinematic + timing profile.

    Returns a dict with ``human_config`` (motor/scroll physics knobs consumed by
    ``interaction_primitives``), ``typing`` overrides, a ``fatigue`` block (slow
    intra-session drift), and an ``identity`` summary. Every numeric knob is drawn
    via :func:`_stable_noisy` so it is reproducible-per-identity but freshly varied
    per session.

    The chosen ranges are anchored to published human-motor / HCI values:

    - ``fitts_a_ms`` / ``fitts_b_ms``: Fitts's law intercept and slope. Human
      pointing throughput ``1/b`` is typically ~3.7–8 bits/s (Card, English &
      Burr, 1978; MacKenzie, 1992), i.e. ``b`` ≈ 125–270 ms/bit, with a small
      non-informational intercept ``a``.
    - ``tremor_hz`` / ``tremor_amp_px``: physiological hand tremor is a roughly
      8–12 Hz oscillation always superimposed on voluntary movement (Elble &
      Koller, 1990, *Tremor*; McAuley & Marsden, 2000, *Brain*).
    - ``power_law_gain``: strength of the 2/3 power law (Lacquaniti et al., 1983).
    - ``corrective_submovement_chance`` / ``_max``: the optimized-submovement model
      (Meyer et al., 1988, *Psychol. Rev.*) — aimed movements are a primary
      ballistic phase plus one or more corrective submovements near the target,
      more frequent for higher-difficulty acquisitions.
    - typing: silent reading ~238 wpm (Brysbaert, 2019, *J. Mem. Lang.*); inter-key
      latency and key-hold times vary per typist with digraph dependence
      (Dhakal et al., 2018, *CHI*, "Observations on Typing from 136M Keystrokes";
      Killourhy & Maxion, 2009).
    """
    s = _stable_noisy

    human_config: dict[str, Any] = {
        # Fitts's law movement-time parameters (per-person intercept/slope).
        "fitts_a_ms": s(identity_rng, session_rng, lo=70.0, hi=150.0, session_jitter=0.10),
        "fitts_b_ms": s(identity_rng, session_rng, lo=120.0, hi=230.0, session_jitter=0.10),
        # Per-sample emission cadence; total step count is derived from Fitts MT.
        "mouse_step_delay_ms": s(identity_rng, session_rng, lo=6.0, hi=14.0, session_jitter=0.12),
        "mouse_step_delay_cv": s(identity_rng, session_rng, lo=0.30, hi=0.60, session_jitter=0.10),
        "mouse_max_steps": int(round(s(identity_rng, session_rng, lo=28.0, hi=40.0, session_jitter=0.06))),
        # Curvature of the ballistic arc (control-point bow as a fraction of travel).
        "mouse_curve_bow": s(identity_rng, session_rng, lo=0.07, hi=0.20, session_jitter=0.15),
        # 8–12 Hz physiological tremor superimposed on the path.
        "tremor_hz": s(identity_rng, session_rng, lo=8.0, hi=12.0, session_jitter=0.06),
        "tremor_amp_px": s(identity_rng, session_rng, lo=0.20, hi=1.10, session_jitter=0.20),
        # 2/3 power-law application strength.
        "power_law_gain": s(identity_rng, session_rng, lo=0.55, hi=1.0, session_jitter=0.10),
        # Minimum-jerk velocity-profile asymmetry (slightly longer deceleration).
        "min_jerk_skew": s(identity_rng, session_rng, lo=0.0, hi=0.22, session_jitter=0.20),
        # Endpoint accuracy and overshoot/correction tendencies.
        "mouse_imprecision_px": s(identity_rng, session_rng, lo=1.5, hi=5.0, session_jitter=0.18),
        "mouse_overshoot_chance": s(identity_rng, session_rng, lo=0.04, hi=0.16, session_jitter=0.18),
        "corrective_submovement_chance": s(identity_rng, session_rng, lo=0.30, hi=0.70, session_jitter=0.15),
        "corrective_submovement_max": int(round(s(identity_rng, session_rng, lo=1.0, hi=2.0, session_jitter=0.0))),
        # Scroll-momentum personality.
        "scroll_max_steps": int(round(s(identity_rng, session_rng, lo=5.0, hi=11.0, session_jitter=0.10))),
        "scroll_decay": s(identity_rng, session_rng, lo=0.45, hi=0.75, session_jitter=0.10),
        "scroll_step_pause_ms": s(identity_rng, session_rng, lo=38.0, hi=80.0, session_jitter=0.15),
        "scroll_step_pause_cv": s(identity_rng, session_rng, lo=0.30, hi=0.55, session_jitter=0.10),
        "scroll_settle_ms": s(identity_rng, session_rng, lo=160.0, hi=320.0, session_jitter=0.15),
    }

    typing: dict[str, Any] = {
        # Per-typist mean inter-key latency window and typo propensity. Dhakal et
        # al. (2018) report mean inter-key intervals clustering ~120–240 ms across a
        # wide population; faster typists sit lower.
        "base_interkey_ms": s(identity_rng, session_rng, lo=95.0, hi=210.0, session_jitter=0.12),
        "interkey_cv": s(identity_rng, session_rng, lo=0.30, hi=0.50, session_jitter=0.10),
        # Key-hold (dwell) time, the press-to-release interval; ~70–120 ms typical.
        "key_hold_ms": s(identity_rng, session_rng, lo=70.0, hi=120.0, session_jitter=0.15),
        "typo_chance": s(identity_rng, session_rng, lo=0.01, hi=0.05, session_jitter=0.20),
    }

    fatigue: dict[str, Any] = {
        # Slow within-session drift (fatigue/adaptation): by the end of a long
        # session, movement time, tremor and endpoint scatter grow modestly while
        # the operator gets a little slower/sloppier. ``half_actions`` is the number
        # of actions at which roughly half of the configured drift is reached.
        "enabled": True,
        "max_slowdown": s(identity_rng, session_rng, lo=0.08, hi=0.22, session_jitter=0.20),
        "max_sloppiness": s(identity_rng, session_rng, lo=0.10, hi=0.35, session_jitter=0.20),
        "half_actions": int(round(s(identity_rng, session_rng, lo=40.0, hi=90.0, session_jitter=0.10))),
    }

    identity_summary = {
        "fitts_b_ms": round(human_config["fitts_b_ms"], 1),
        "tremor_hz": round(human_config["tremor_hz"], 2),
        "overshoot_chance": round(human_config["mouse_overshoot_chance"], 3),
        "base_interkey_ms": round(typing["base_interkey_ms"], 1),
    }

    return {
        "human_config": human_config,
        "typing": typing,
        "fatigue": fatigue,
        "identity": identity_summary,
    }


def _apply_human_config_overrides(human_config: dict[str, Any], target: dict[str, Any]) -> None:
    """Let explicit ``target`` values pin individual physics knobs over identity defaults.

    Only keys actually present (and parseable) on ``target`` override the
    identity-derived baseline; everything else keeps its stable-but-noisy value.
    """
    float_keys = {
        "fitts_a_ms": (40.0, 400.0),
        "fitts_b_ms": (80.0, 400.0),
        "mouse_step_delay_ms": (1.0, 50.0),
        "mouse_step_delay_cv": (0.0, 2.0),
        "mouse_curve_bow": (0.0, 0.5),
        "tremor_hz": (5.0, 14.0),
        "tremor_amp_px": (0.0, 3.0),
        "power_law_gain": (0.0, 1.0),
        "min_jerk_skew": (0.0, 0.45),
        "mouse_imprecision_px": (0.0, 12.0),
        "mouse_overshoot_chance": (0.0, 0.5),
        "corrective_submovement_chance": (0.0, 0.95),
        "scroll_decay": (0.2, 0.95),
        "scroll_step_pause_ms": (4.0, 400.0),
        "scroll_step_pause_cv": (0.0, 2.0),
        "scroll_settle_ms": (20.0, 1500.0),
    }
    for key, (lo, hi) in float_keys.items():
        if key in target:
            human_config[key] = _bounded_float(target.get(key), lower=lo, upper=hi, default=human_config.get(key, lo))
    int_keys = {
        "mouse_max_steps": (12, 60),
        "scroll_max_steps": (1, 20),
        "corrective_submovement_max": (0, 3),
    }
    for key, (lo_i, hi_i) in int_keys.items():
        if key in target:
            human_config[key] = _bounded_int(target.get(key), lower=lo_i, upper=hi_i, default=int(human_config.get(key, lo_i)))


def _build_typing_block(target: dict[str, Any], identity_typing: dict[str, Any]) -> dict[str, Any]:
    """Merge identity typing defaults with explicit target overrides.

    Emits both the digraph-aware keys consumed by the rewritten ``type_text``
    (``base_interkey_ms``, ``interkey_cv``, ``key_hold_ms``, ``typo_chance``) and the
    legacy ``min_delay_ms``/``max_delay_ms`` window so older callers/tests keep
    working. Inter-key latency and key-hold/dwell vary per typist with digraph
    dependence (Dhakal et al., 2018; Killourhy & Maxion, 2009).
    """
    base_interkey = _bounded_float(
        target.get("typing_base_interkey_ms", identity_typing.get("base_interkey_ms")),
        lower=40.0,
        upper=400.0,
        default=140.0,
    )
    interkey_cv = _bounded_float(
        target.get("typing_interkey_cv", identity_typing.get("interkey_cv")),
        lower=0.05,
        upper=1.0,
        default=0.4,
    )
    key_hold = _bounded_float(
        target.get("typing_key_hold_ms", identity_typing.get("key_hold_ms")),
        lower=20.0,
        upper=250.0,
        default=95.0,
    )
    typo_chance = _bounded_float(
        target.get("typing_typo_chance", identity_typing.get("typo_chance")),
        lower=0.0,
        upper=0.08,
        default=0.015,
    )
    # Legacy window kept for backward compatibility (rewritten type_text prefers the
    # digraph-aware base_interkey_ms but falls back to this band).
    min_delay = _bounded_int(target.get("typing_min_delay_ms"), lower=20, upper=400, default=int(round(base_interkey * 0.55)))
    max_delay = _bounded_int(target.get("typing_max_delay_ms"), lower=45, upper=800, default=int(round(base_interkey * 1.9)))
    return {
        "base_interkey_ms": base_interkey,
        "interkey_cv": interkey_cv,
        "key_hold_ms": key_hold,
        "min_delay_ms": min_delay,
        "max_delay_ms": max(min_delay, max_delay),
        "typo_chance": typo_chance,
        "correction_chance": _bounded_float(target.get("typing_correction_chance"), lower=0.0, upper=1.0, default=0.92),
        "pause_mid_query_chance": _bounded_float(target.get("typing_pause_mid_query_chance"), lower=0.0, upper=0.35, default=0.08),
    }


def build_behavior_plan(
    target: dict[str, Any],
    *,
    provider: str,
    goal: Any = "observe_visible_page_state",
    seed: str | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe pacing and interaction plan for one automation run.

    ``run_id`` is ALWAYS fresh per call so two runs never share an entropy seed by
    default ("random like a human": full per-session stochasticity). An explicit
    ``seed`` makes the session reproducible for debugging — it is threaded onto the
    plan as ``session_seed`` and consumed by the single advancing per-session RNG in
    ``interaction_primitives``; when ``seed`` is None the primitives draw fresh OS
    entropy so sessions genuinely differ.

    ``target["identity_id"]`` (optional) names a persistent person: their persona and
    kinematic baseline are derived deterministically from it (stable across sessions)
    while each session perturbs those baselines slightly, so the same identity is
    self-similar but never identical and distinct identities are clearly separated
    (cross-session anti-clustering; see :func:`build_identity_kinematics`).
    """
    run_id = secrets.token_hex(8)
    session_seed = None if seed is None else str(seed)
    # Per-session RNG used for plan-build draws and per-session kinematic jitter.
    rng = random.Random(seed or f"{run_id}:{target.get('id')}:{provider}")

    # Identity: stable across an identity's sessions when ``identity_id`` is given,
    # otherwise a one-off identity keyed to this run.
    identity_id = target.get("identity_id")
    if identity_id is not None and str(identity_id).strip():
        identity_rng = random.Random(f"identity:{identity_id}")
        persona = identity_rng.choice(PERSONAS)
    else:
        identity_id = None
        identity_rng = random.Random(f"identity:{run_id}")
        persona = rng.choice(PERSONAS)

    kinematics = build_identity_kinematics(identity_rng, rng)
    ladder = _configured_completion_ladder(target)
    completion = _choose_completion(rng, ladder)
    goal_payload = _goal_payload(goal)
    device_mode = str(target.get("automation_device") or target.get("device_mode") or "desktop").strip().lower()
    mobile_enabled = device_mode == "mobile" or _bool_from_target(target, "mobile_gestures", False)

    base_wait = _env_int("AUTOMATION_CONNECTOR_WAIT_MS", DEFAULT_WAIT_MS)
    base_scroll = _env_int("AUTOMATION_CONNECTOR_SCROLL_DELTA_Y", DEFAULT_SCROLL_DELTA_Y)
    scroll_passes = min(completion.max_scroll_passes, MAX_SCROLL_PASSES)
    # Per-pass human wait/think times are sampled from a log-normal (right-skewed
    # like real reading pauses) centered on the persona-scaled base wait, instead
    # of a flat uniform band.
    waits = [
        int(lognormal_ms(rng, mean_ms=base_wait * persona.dwell_multiplier, cv=0.55, lo=250, hi=12_000))
        for _ in range(max(1, scroll_passes))
    ]
    # Scroll deltas are pixel magnitudes (not timing); keep them positive and
    # let callers signal direction via the sign of the fallback in
    # planned_scroll_delta.
    scroll_deltas = [
        _bounded_ms(base_scroll * persona.scroll_multiplier * rng.uniform(0.5, 1.6), lower=120, upper=2_000)
        for _ in range(max(0, scroll_passes - 1))
    ]

    # Identity-driven physics/typing/fatigue knobs. Explicit target overrides win
    # over the identity defaults so callers can still pin specific values.
    human_config = dict(kinematics["human_config"])
    _apply_human_config_overrides(human_config, target)
    typing_block = _build_typing_block(target, kinematics["typing"])

    return {
        "policy_version": POLICY_VERSION,
        "run_id": run_id,
        # None => primitives use fresh OS entropy per session (default). A string
        # seed => the whole session's RNG stream is reproducible for debugging.
        "session_seed": session_seed,
        "identity_id": identity_id,
        "identity": kinematics["identity"],
        "human_config": human_config,
        "fatigue": kinematics["fatigue"],
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
        "typing": typing_block,
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
        "identity_id": plan.get("identity_id"),
        "identity": plan.get("identity") if isinstance(plan.get("identity"), dict) else {},
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
    """Return a planned scroll delta, HONORING the sign of `fallback`.

    The plan's pacing list stores positive pixel magnitudes; the caller signals
    direction through the sign of `fallback` (negative => scroll up / re-read).
    We take the magnitude from the plan (or the fallback magnitude) and re-apply
    the caller's sign so scroll-up requests are not silently flipped to scroll-down.
    """
    sign = -1 if fallback < 0 else 1
    pacing = plan.get("pacing") if isinstance(plan, dict) else None
    deltas = pacing.get("scroll_delta_y") if isinstance(pacing, dict) else None
    if isinstance(deltas, list) and deltas:
        magnitude = _bounded_int(
            abs(deltas[min(pass_index, len(deltas) - 1)]),
            lower=50,
            upper=2_000,
            default=abs(fallback),
        )
        return sign * magnitude
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
