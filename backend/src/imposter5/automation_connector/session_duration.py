"""Human session-DURATION model for Imposter5 campaigns.

The arrival clock (``arrival_clock.py``) answers *when* a session starts; this
answers *how long it lasts*. A real person's session lengths are not one fixed
budget — they are heavy-tailed and multi-modal:

  * many sub-minute "dip in, check one thing, leave" visits,
  * a typical few-minute browse,
  * an occasional long read.

A scheduler that runs every session for the same ~4 minutes leaves a spike in
the session-duration histogram that a detector can see even when every
within-session mechanic is perfect (low coefficient of variation, no short
dips, a pile-up at one length). This is the duration analogue of the
fixed-period arrival tell the arrival clock fixed.

This module samples a per-session duration (seconds) from a mixture of lognormal
modes, clamped to the connector's contract window [15, 900]s (see
``automation_connector.models.AutomationConnectorTargetRequest``). Pure and
seedable so a run replays; ``generate_durations`` mirrors
``arrival_clock.generate_stream`` for diagnostics and the Red-vs-Blue matchup.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# Connector contract window (AutomationConnectorTargetRequest.gauntlet_duration_s).
MIN_SECONDS = 15.0
MAX_SECONDS = 900.0

# Time-of-day coupling of session LENGTH (index 0..23, local hour). A real
# person's visit length is not the same at 03:00 as at 20:00: daytime/work-hour
# visits are squeezed quick dips, while the evening unwind is a longer browse and
# the rare deep-night check is the briefest of all. Without this, the duration
# histogram is identical around the clock — a tell that survives even a perfect
# per-session walk. These are gentle multipliers on the duration draw (centered
# near 1.0, bounded so no hour collapses or explodes the length).
_TIME_OF_DAY_DURATION_SCALE = (
    0.70, 0.66, 0.66, 0.70, 0.74, 0.82,  # 00-05 deep night: rare, brief glances
    0.90, 0.92, 0.90, 0.88, 0.90, 0.96,  # 06-11 morning + work: quick dips
    1.06, 0.92, 0.90, 0.92, 0.98, 1.08,  # 12-17 lunch a touch longer, afternoon dips
    1.22, 1.28, 1.24, 1.14, 1.00, 0.84,  # 18-23 evening unwind: the long browse, then taper
)


def time_of_day_scale(local_hour: int) -> float:
    """Duration multiplier for a given local hour (0..23).

    Quick dips during the work day, longer browses in the evening, briefest in
    the deep night. Multiply into :func:`sample_session_seconds`' ``scale`` so a
    scheduled identity's session lengths track the local clock instead of being
    drawn from one time-invariant distribution.
    """
    return _TIME_OF_DAY_DURATION_SCALE[int(local_hour) % 24]


@dataclass(frozen=True)
class DurationMode:
    """One lognormal mode of the session-length mixture."""

    weight: float
    median_s: float
    sigma: float


@dataclass(frozen=True)
class DurationParams:
    """A heavy-tailed, multi-modal human session-length mixture.

    Defaults target an ordinary feed-checker: a third of visits are short dips,
    half are a few-minute browse, and a long tail runs toward the 15-minute cap.
    """

    modes: tuple[DurationMode, ...] = (
        # "dip in, check one thing, leave" — the micro-session.
        DurationMode(weight=0.32, median_s=28.0, sigma=0.45),
        # the ordinary few-minute browse
        DurationMode(weight=0.50, median_s=190.0, sigma=0.52),
        # the occasional long read (tail toward the 15-min cap)
        DurationMode(weight=0.18, median_s=560.0, sigma=0.42),
    )
    # A persona's dwell gently scales the draw; clamp so it never flattens the
    # shape into a new fixed length.
    scale_lo: float = 0.6
    scale_hi: float = 1.6


DEFAULT_DURATION_PARAMS = DurationParams()


def _pick_mode(rng: random.Random, params: DurationParams) -> DurationMode:
    total = sum(m.weight for m in params.modes)
    r = rng.random() * total
    acc = 0.0
    for mode in params.modes:
        acc += mode.weight
        if r <= acc:
            return mode
    return params.modes[-1]


def sample_session_seconds(
    rng: random.Random,
    *,
    scale: float = 1.0,
    params: DurationParams = DEFAULT_DURATION_PARAMS,
) -> float:
    """Draw one human-plausible session duration in seconds, clamped to [15, 900].

    A mixture of lognormal modes: most draws are short dips or a few-minute
    browse, with a heavy tail of long sessions. ``scale`` (e.g. a persona's dwell
    multiplier) gently stretches or compresses the draw without changing the
    overall shape.
    """
    mode = _pick_mode(rng, params)
    draw = math.exp(rng.gauss(math.log(mode.median_s), mode.sigma))
    bounded_scale = max(params.scale_lo, min(params.scale_hi, float(scale)))
    return float(max(MIN_SECONDS, min(MAX_SECONDS, bounded_scale * draw)))


def generate_durations(
    n: int,
    *,
    seed: str | int | None = None,
    scale: float = 1.0,
    params: DurationParams = DEFAULT_DURATION_PARAMS,
) -> list[float]:
    """Offline draw of ``n`` session durations (for diagnostics / the matchup).

    This is the *same* per-session process the planner uses, so a histogram over
    this stream characterizes what a detector would see in production.
    """
    rng = random.Random(seed)
    return [sample_session_seconds(rng, scale=scale, params=params) for _ in range(n)]
