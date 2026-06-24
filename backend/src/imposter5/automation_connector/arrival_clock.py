"""Human-like campaign arrival times (workstream D, anti-fingerprint).

The old scheduler returned ``next_run_at = last + interval_minutes`` and the
worker polled on a 60 s grid. That produced three stacked tells the blue team
fingerprinted:

1. a *constant period* (a sharp spike in the periodogram);
2. *whole-minute / whole-second* spawn instants (the poll grid);
3. *no circadian shape* — equally likely at 03:00 as at 14:00.

Per-event jitter alone does **not** fix this. If gaps are drawn independently
and identically, the process is a flat renewal process: zoomed in each gap looks
human, but zoomed out the counts over-average (Fano factor flattens at the
gaps' CV^2), there is no memory, and the spectrum is structureless. Real human
activity is *over-dispersed* and *bursty* with *long-range dependence*.

So this module is a hierarchical generative model that is correct at every
scale by construction, applied **online** (so the deployed stream — not just an
offline simulation — carries the structure):

- **Macro (days):** a slowly drifting latent log-rate (an AR(1)/Ornstein-
  Uhlenbeck process) modulates the gap. Persistent drift gives counts that keep
  spreading as the window grows (rising Fano, Hurst > 0.5) instead of averaging
  out.
- **Meso (hours):** sessions are placed by *thinning* against a circadian
  intensity shaped by a per-persona **chronotype** in the persona's timezone,
  plus light **self-excitation** (Hawkes-style bursts) so sessions cluster.
- **Micro (seconds):** heavy-tailed (lognormal) gaps and genuine sub-second
  offsets, so spawns never land on the minute grid.

The online step :func:`advance` carries a small JSON-serializable
:class:`ArrivalState` (the latent rate + burst counters + per-identity seed) so
the long-range structure survives across separate worker invocations. The
offline :func:`generate_stream` iterates the same step, so the diagnostics in
``arrival_diagnostics`` validate exactly what ships.

This module is intentionally stdlib-only: it runs inside the scheduler worker.
"""
from __future__ import annotations

import functools
import math
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Chronotypes — relative activity intensity per local hour (index 0..23).
# Values are unnormalized weights; only their ratios matter (thinning uses
# weight / max_weight as an acceptance probability).
# --------------------------------------------------------------------------- #
_NINE_TO_FIVE = (
    0.02, 0.01, 0.01, 0.01, 0.01, 0.03,  # 00-05 night
    0.10, 0.30, 0.70, 1.00, 1.00, 0.95,  # 06-11 morning ramp + peak
    0.70, 0.90, 1.00, 0.95, 0.85, 0.70,  # 12-17 lunch dip then afternoon
    0.45, 0.30, 0.22, 0.15, 0.08, 0.04,  # 18-23 evening taper
)
_EARLY_BIRD = (
    0.03, 0.01, 0.01, 0.01, 0.04, 0.20,  # wakes early
    0.60, 1.00, 1.00, 0.90, 0.80, 0.70,
    0.60, 0.65, 0.60, 0.50, 0.40, 0.30,
    0.22, 0.15, 0.10, 0.06, 0.03, 0.02,
)
_NIGHT_OWL = (
    0.35, 0.25, 0.15, 0.08, 0.04, 0.02,  # active into the small hours
    0.03, 0.06, 0.12, 0.25, 0.40, 0.50,
    0.55, 0.60, 0.65, 0.70, 0.78, 0.85,
    0.95, 1.00, 1.00, 0.90, 0.70, 0.50,
)
_EVENING = (
    0.04, 0.02, 0.01, 0.01, 0.01, 0.03,  # after-work peak
    0.08, 0.20, 0.35, 0.45, 0.45, 0.45,
    0.50, 0.45, 0.45, 0.50, 0.60, 0.80,
    1.00, 1.00, 0.95, 0.75, 0.45, 0.18,
)
_FLAT = tuple(1.0 for _ in range(24))  # baseline: no circadian shape (testing/non-human)

_CHRONOTYPES: dict[str, tuple[float, ...]] = {
    "nine_to_five": _NINE_TO_FIVE,
    "early_bird": _EARLY_BIRD,
    "night_owl": _NIGHT_OWL,
    "evening": _EVENING,
    "flat": _FLAT,
}

# Persona name -> chronotype. Unknown personas fall back to nine_to_five.
_PERSONA_CHRONOTYPE: dict[str, str] = {
    "focused_power_user": "nine_to_five",
    "methodical_operator": "nine_to_five",
    "impatient_scanner": "nine_to_five",
    "curious_reader": "evening",
    "slow_reader": "evening",
    "mobile_checker": "early_bird",
    "late_day_review": "night_owl",
}

DEFAULT_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class CircadianProfile:
    """When a given identity is plausibly active."""

    timezone: str = DEFAULT_TIMEZONE
    chronotype: str = "nine_to_five"
    weekend_scale: float = 0.65  # weekday worker is quieter on weekends

    @property
    def hour_weights(self) -> tuple[float, ...]:
        return _CHRONOTYPES.get(self.chronotype, _NINE_TO_FIVE)

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo("UTC")


def profile_for_persona(
    persona_name: str | None,
    *,
    timezone: str | None = None,
    weekend_scale: float | None = None,
) -> CircadianProfile:
    """Map a persona name to a circadian profile, with overridable timezone."""
    chronotype = _PERSONA_CHRONOTYPE.get((persona_name or "").strip(), "nine_to_five")
    return CircadianProfile(
        timezone=timezone or DEFAULT_TIMEZONE,
        chronotype=chronotype,
        weekend_scale=0.65 if weekend_scale is None else float(weekend_scale),
    )


@dataclass(frozen=True)
class ArrivalParams:
    """Tunable knobs for the generative model. Defaults target a desk worker.

    All knobs are dimensionless multipliers on the nominal ``interval_minutes``
    except the OU/burst shape parameters, so a campaign keeps its requested
    average cadence while gaining human structure.
    """

    # Micro: marginal gap shape. Lognormal sigma sets the heavy tail; larger ->
    # higher CV -> more over-dispersion at small scales.
    gap_sigma: float = 0.62
    gap_floor_frac: float = 0.12  # a gap is never shorter than 12% of nominal
    gap_cap_frac: float = 9.0     # ...nor longer than 9x (before circadian thinning)

    # Macro: AR(1)/OU drift on log-rate. phi near 1 -> persistent drift ->
    # long-range dependence (rising Fano, Hurst > 0.5). sigma_ou sets its scale.
    ou_phi: float = 0.88
    ou_sigma: float = 0.28
    ou_clip: float = 1.4  # clamp |log_rate| so drift cannot explode the cadence

    # Meso: self-excitation. With prob trigger, open a short burst of clustered
    # sessions; burst length is geometric with the given mean.
    burst_trigger: float = 0.22
    burst_mean_len: float = 2.4
    burst_gap_frac: float = 0.28  # within-burst gaps are short

    # Occasional dead periods (skipped windows / quiet days).
    skip_prob: float = 0.06
    skip_mult_lo: float = 3.0
    skip_mult_hi: float = 7.0

    # Rare MULTI-DAY absences (vacation / sick / travel). Distinct from the
    # in-day ``skip`` above: when this fires the identity goes dark for *days*
    # then returns. A metronome never takes a holiday; a human does, and a long
    # capture with no multi-day holes is itself a tell. Kept rare so the
    # campaign's average cadence is essentially preserved (the bulk of gaps stay
    # lognormal; these are a thin extreme tail).
    away_prob: float = 0.008
    away_days_lo: float = 1.0
    away_days_hi: float = 3.0

    # Per-day variation in the circadian SHAPE. A real person's hour-by-hour
    # activity is not a pixel-identical template repeated every day; some days
    # run later, some have a flatter midday. ``day_shape_sigma`` is the lognormal
    # spread of a per-(identity, local-day) multiplier on each hour's weight; the
    # clamps stop a day from flattening to noise or spiking to always-on. 0 here
    # restores the old fixed template.
    day_shape_sigma: float = 0.22
    day_shape_lo: float = 0.6
    day_shape_hi: float = 1.5

    # Circadian thinning grid.
    thinning_step_minutes: float = 22.0
    thinning_max_iter: int = 96  # enough to cross an overnight dead zone


DEFAULT_PARAMS = ArrivalParams()


@dataclass(frozen=True)
class ArrivalState:
    """Per-identity latent state carried across worker invocations.

    JSON-serializable so :class:`TaskStore` can persist it on the task record.
    ``seed`` gives each identity an independent stream (no synchronized fleet),
    while ``step`` makes each draw reproducible for tests and auditing.
    """

    seed: str
    step: int = 0
    log_rate: float = 0.0
    burst_remaining: int = 0

    @classmethod
    def initial(cls, seed: str | None = None) -> "ArrivalState":
        return cls(seed=seed or new_seed())

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "step": self.step,
            "log_rate": self.log_rate,
            "burst_remaining": self.burst_remaining,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ArrivalState":
        if not isinstance(raw, dict) or not raw.get("seed"):
            return cls.initial()
        return cls(
            seed=str(raw["seed"]),
            step=int(raw.get("step", 0)),
            log_rate=float(raw.get("log_rate", 0.0)),
            burst_remaining=int(raw.get("burst_remaining", 0)),
        )


def new_seed() -> str:
    """A fresh per-identity entropy seed drawn from the OS CSPRNG."""
    return random.SystemRandom().randbytes(16).hex()


# --------------------------------------------------------------------------- #
# Core online step
# --------------------------------------------------------------------------- #
def advance(
    state: ArrivalState,
    *,
    now: datetime,
    interval_minutes: float,
    profile: CircadianProfile = CircadianProfile(),
    params: ArrivalParams = DEFAULT_PARAMS,
) -> tuple[datetime, ArrivalState]:
    """Compute the next human-plausible arrival after ``now`` and the new state.

    Pure and deterministic given ``(state.seed, state.step)``: the same identity
    replays the same stream, while different seeds are independent.
    """
    now = _as_utc(now)
    rng = random.Random(f"{state.seed}:{state.step}")

    # Macro: drift the latent log-rate (mean-reverting AR(1)).
    log_rate = params.ou_phi * state.log_rate + params.ou_sigma * rng.gauss(0.0, 1.0)
    log_rate = max(-params.ou_clip, min(params.ou_clip, log_rate))

    # Meso/micro: choose this gap as either a within-burst short gap or a fresh
    # heavy-tailed draw that may itself open a new burst.
    burst_remaining = state.burst_remaining
    if burst_remaining > 0:
        gap_frac = params.burst_gap_frac * math.exp(0.25 * rng.gauss(0.0, 1.0))
        burst_remaining -= 1
    else:
        # Lognormal with unit median; subtract sigma^2/2 to keep the mean ~1 so
        # the campaign's requested average cadence is preserved.
        gap_frac = math.exp(params.gap_sigma * rng.gauss(0.0, 1.0))
        if rng.random() < params.burst_trigger:
            burst_remaining = _geometric_len(rng, params.burst_mean_len)

    gap_frac = max(params.gap_floor_frac, min(params.gap_cap_frac, gap_frac))
    if rng.random() < params.skip_prob:
        gap_frac *= rng.uniform(params.skip_mult_lo, params.skip_mult_hi)

    gap_minutes = interval_minutes * gap_frac * math.exp(log_rate)

    # Rare multi-day absence (vacation / sick / travel) layered ON TOP of the
    # ordinary gap. The circadian thinning below then re-seats the *return* into
    # a plausible active hour, so the identity comes back mid-morning rather than
    # at 04:00. Rare by design, so the marginal gap law stays lognormal in bulk.
    if rng.random() < params.away_prob:
        gap_minutes += rng.uniform(params.away_days_lo, params.away_days_hi) * 1440.0

    candidate = now + timedelta(seconds=gap_minutes * 60.0)

    # Meso: thin against the circadian intensity so dead hours stay quiet. The
    # per-identity seed also drives the per-day SHAPE wobble (each day's curve
    # differs instead of repeating one fixed template).
    candidate = _apply_circadian(candidate, profile, rng, params, seed=state.seed)

    # Micro: guarantee a sub-second offset; never land on the second/minute grid.
    candidate = _ensure_subsecond(candidate, rng)
    if candidate <= now:
        candidate = now + timedelta(seconds=max(1.0, gap_minutes * 60.0))

    new_state = replace(
        state,
        step=state.step + 1,
        log_rate=log_rate,
        burst_remaining=burst_remaining,
    )
    return candidate, new_state


def generate_stream(
    *,
    start: datetime,
    days: float,
    interval_minutes: float,
    profile: CircadianProfile = CircadianProfile(),
    seed: str | None = None,
    params: ArrivalParams = DEFAULT_PARAMS,
    max_events: int | None = None,
) -> list[datetime]:
    """Iterate :func:`advance` over a span. This is the *same* process the
    scheduler deploys, so diagnostics over this stream validate production."""
    start = _as_utc(start)
    end = start + timedelta(days=days)
    state = ArrivalState.initial(seed)
    out: list[datetime] = []
    now = start
    cap = max_events if max_events is not None else int(days * 400) + 64
    while now < end and len(out) < cap:
        nxt, state = advance(
            state, now=now, interval_minutes=interval_minutes, profile=profile, params=params
        )
        if nxt >= end:
            break
        out.append(nxt)
        now = nxt
    return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _geometric_len(rng: random.Random, mean_len: float) -> int:
    """Geometric burst length with the given mean (>= 1)."""
    mean_len = max(1.0, mean_len)
    p = 1.0 / mean_len
    u = max(1e-12, rng.random())
    return max(1, int(math.ceil(math.log(u) / math.log(1.0 - p)))) if p < 1.0 else 1


@functools.lru_cache(maxsize=8192)
def _daily_hour_multipliers(
    seed: str, local_date_ordinal: int, sigma: float, lo: float, hi: float
) -> tuple[float, ...]:
    """A per-(identity, local-day) multiplicative wobble on each hour's circadian
    weight, so the activity SHAPE differs from one day to the next instead of
    repeating one fixed template.

    A light AR(1) across the 24 hours keeps adjacent hours correlated, so a day
    shifts a *block* (e.g. "started later today") rather than sprouting isolated
    single-hour spikes. Deterministic in ``(seed, date)`` so a stream replays
    exactly, and memoized because a single ``advance`` may probe several hours.
    """
    if sigma <= 0.0:
        return tuple(1.0 for _ in range(24))
    r = random.Random(f"{seed}|dayshape|{local_date_ordinal}")
    out: list[float] = []
    prev = 0.0
    for _ in range(24):
        prev = 0.6 * prev + 0.4 * r.gauss(0.0, 1.0)
        out.append(max(lo, min(hi, math.exp(sigma * prev))))
    return tuple(out)


def _circadian_weight(
    profile: CircadianProfile,
    dt_utc: datetime,
    *,
    seed: str | None = None,
    params: ArrivalParams = DEFAULT_PARAMS,
) -> float:
    local = dt_utc.astimezone(profile.tz)
    weight = profile.hour_weights[local.hour]
    if seed is not None and params.day_shape_sigma > 0.0:
        mult = _daily_hour_multipliers(
            seed, local.toordinal(),
            params.day_shape_sigma, params.day_shape_lo, params.day_shape_hi,
        )
        weight *= mult[local.hour]
    if local.weekday() >= 5:
        weight *= profile.weekend_scale
    return weight


def _apply_circadian(
    candidate: datetime,
    profile: CircadianProfile,
    rng: random.Random,
    params: ArrivalParams,
    *,
    seed: str | None = None,
) -> datetime:
    """Accept ``candidate`` with probability proportional to circadian intensity;
    otherwise roll forward on a fine grid until an active window is reached.

    The acceptance bound ``w_max`` is the *base* template's max (NOT inflated by
    the day wobble), so the existing cadence/density is preserved: a day that
    runs hotter than the template at some hour simply saturates that hour at full
    acceptance (ratio >= 1) rather than thinning every other hour down. The
    overnight dead zone still stays quiet because its tiny base weight survives
    even a hi-side wobble.
    """
    weights = profile.hour_weights
    w_max = max(weights) * max(1.0, profile.weekend_scale)
    if w_max <= 0:
        return candidate
    for _ in range(params.thinning_max_iter):
        weight = _circadian_weight(profile, candidate, seed=seed, params=params)
        if rng.random() <= weight / w_max:
            return candidate
        step = params.thinning_step_minutes * (0.5 + rng.random())
        candidate = candidate + timedelta(minutes=step)
    return candidate


def _ensure_subsecond(candidate: datetime, rng: random.Random) -> datetime:
    """Never let a spawn land exactly on a whole second (the poll-grid tell)."""
    if candidate.microsecond == 0:
        candidate = candidate.replace(microsecond=rng.randint(1, 999_999))
    return candidate
