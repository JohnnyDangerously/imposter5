"""Humanization distributions for automation-connector interaction primitives.

These are the small statistical/biomechanical building blocks used by
``interaction_primitives`` to make timing and motion conform to published laws of
human motor control and human timing rather than to uniform/constant generators:

- ``lognormal_ms``: heavy-right-tailed delays (keystrokes, inter-step mouse waits,
  scroll-step pauses). Humans have a most-common short delay with occasional long
  pauses, which a log-normal models far better than ``uniform``. The log-normal is
  also the marginal of Plamondon's kinematic (Sigma-lognormal) theory of rapid
  human movements (Plamondon, 1995, *Biol. Cybern.*).
- ``weibull_ms``: an alternative right-skewed delay with an independent shape knob,
  used for occasional "distraction"/idle inter-action gaps. Human response- and
  inter-action-time distributions are classically fit by Weibull / ex-Gaussian
  forms (Luce, 1986, *Response Times*; Van Zandt, 2000).
- ``fitts_movement_time_ms``: Fitts's law movement time, MT = a + b·log2(D/W + 1)
  (Fitts, 1954, *J. Exp. Psychol.*; Shannon formulation, MacKenzie, 1992, *HCI*).
- ``min_jerk_progress``: the minimum-jerk position profile s(τ)=10τ³−15τ⁴+6τ⁵
  (Flash & Hogan, 1985, *J. Neurosci.*); its derivative is the canonical bell-shaped
  velocity profile of point-to-point human reaching.
- ``two_thirds_power_dwell_scale``: the 2/3 power law — tangential velocity scales
  with curvature^(-1/3) along curved paths (Lacquaniti, Terzuolo & Viviani, 1983,
  *Acta Psychol.*), expressed here as a per-sample dwell multiplier so the cursor
  slows through high-curvature segments.
- ``scroll_decay_deltas``: a wheel-burst that starts fast and geometrically decays,
  the way a real flick/scroll bleeds off momentum, summing (approximately) to a
  requested total and preserving direction (sign).

Each takes a ``random.Random`` so a single advancing per-session stream produces
fresh, non-repeating draws while remaining reproducible from an explicit seed.
"""
from __future__ import annotations

import math
import random


def lognormal_ms(
    rng: random.Random,
    *,
    mean_ms: float,
    cv: float,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Sample a non-negative delay in milliseconds from a log-normal distribution.

    The distribution is parameterized by its *arithmetic* ``mean_ms`` and coefficient
    of variation ``cv`` (std / mean), which is the intuitive way to express "around
    N ms, this spread". We convert to the underlying normal parameters:

        sigma = sqrt(ln(1 + cv**2))
        mu    = ln(mean_ms) - sigma**2 / 2

    so that ``E[X] == mean_ms``. The sample is then clamped to ``[lo, hi]`` when those
    bounds are provided. The result is always non-negative.
    """
    mean = float(mean_ms)
    if mean <= 0.0:
        # No meaningful positive mean: fall back to the lower bound (or 0).
        base = 0.0 if lo is None else max(0.0, float(lo))
        return base
    c = max(0.0, float(cv))
    sigma = math.sqrt(math.log(1.0 + c * c))
    if sigma <= 0.0:
        # cv == 0 => degenerate distribution at the mean.
        value = mean
    else:
        mu = math.log(mean) - (sigma * sigma) / 2.0
        value = rng.lognormvariate(mu, sigma)
    if lo is not None:
        value = max(float(lo), value)
    if hi is not None:
        value = min(float(hi), value)
    return max(0.0, value)


def weibull_ms(
    rng: random.Random,
    *,
    scale_ms: float,
    shape: float,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Sample a non-negative delay (ms) from a Weibull distribution.

    Human inter-action gaps, reaction times, and "time to next action" are
    classically modeled by right-skewed Weibull / ex-Gaussian forms (Luce, 1986,
    *Response Times*; Van Zandt, 2000, *Psychon. Bull. Rev.*). Unlike the
    log-normal, the Weibull exposes an independent ``shape`` (k) knob: k < 1 gives a
    heavier tail (more "distraction" outliers), k ≈ 1.5 a moderate skew. Sampled via
    inverse-CDF: X = scale·(−ln U)^(1/k).
    """
    k = max(1e-3, float(shape))
    scale = max(0.0, float(scale_ms))
    if scale <= 0.0:
        return 0.0 if lo is None else max(0.0, float(lo))
    u = rng.random()
    u = min(max(u, 1e-12), 1.0 - 1e-12)
    value = scale * (-math.log(u)) ** (1.0 / k)
    if lo is not None:
        value = max(float(lo), value)
    if hi is not None:
        value = min(float(hi), value)
    return max(0.0, value)


def fitts_movement_time_ms(
    distance_px: float,
    target_width_px: float,
    *,
    a_ms: float = 100.0,
    b_ms: float = 140.0,
    min_ms: float = 40.0,
) -> float:
    """Movement time predicted by Fitts's law (Fitts, 1954; MacKenzie, 1992).

    Uses the Shannon formulation ``MT = a + b·log2(D/W + 1)``, where the index of
    difficulty ``ID = log2(D/W + 1)`` grows with the distance-to-target-width ratio:
    far and/or small targets take proportionally longer to acquire. ``a`` (ms) is the
    non-informational intercept and ``b`` (ms/bit) the slope; both vary per person,
    which is why they are surfaced as identity knobs. The result is floored at
    ``min_ms`` so micro-moves still take a plausible non-zero time.
    """
    d = max(0.0, float(distance_px))
    w = max(1.0, float(target_width_px))
    idx = math.log2(d / w + 1.0)
    return max(float(min_ms), float(a_ms) + float(b_ms) * idx)


def min_jerk_progress(tau: float) -> float:
    """Minimum-jerk normalized displacement profile (Flash & Hogan, 1985).

    For normalized time ``τ ∈ [0, 1]``, ``s(τ) = 10τ³ − 15τ⁴ + 6τ⁵`` is the unique
    trajectory minimizing integrated jerk between two rest points. Its time
    derivative is a smooth, symmetric, bell-shaped velocity profile with zero
    velocity and acceleration at both endpoints — the standard model of human
    point-to-point reaching. Used as a time→progress reparameterization so emitted
    cursor samples accelerate, peak, then decelerate like a real reach.
    """
    t = min(1.0, max(0.0, float(tau)))
    return t * t * t * (10.0 - 15.0 * t + 6.0 * t * t)


def two_thirds_power_dwell_scale(
    curvature: float,
    *,
    ref_curvature: float,
    gain: float = 1.0,
    cap: float = 3.0,
) -> float:
    """Per-sample dwell multiplier implementing the 2/3 power law.

    Lacquaniti, Terzuolo & Viviani (1983) found tangential velocity ``V`` along
    curved hand movements scales as ``V ∝ R^(1/3) = κ^(−1/3)`` (κ = curvature = 1/R):
    people slow down through tight curves and speed up on straight segments. Since
    the time spent per unit arc-length is ``∝ 1/V ∝ κ^(1/3)``, a sample sitting at
    curvature ``κ`` should dwell ``(κ / κ_ref)^(1/3·gain)`` times as long as a sample
    at the reference curvature. ``gain`` in [0,1] scales how strongly the law is
    applied; the multiplier is clamped to ``[1/cap, cap]`` to keep step delays sane.
    """
    g = max(0.0, float(gain))
    ref = max(1e-9, float(ref_curvature))
    kappa = max(0.0, float(curvature))
    ratio = (kappa / ref) if ref > 0 else 1.0
    if ratio <= 0.0:
        scale = 1.0 / max(1.0, float(cap))
    else:
        scale = ratio ** (g / 3.0)
    hi = max(1.0, float(cap))
    lo = 1.0 / hi
    return max(lo, min(hi, scale))


def scroll_decay_deltas(
    rng: random.Random,
    *,
    total_px: float,
    max_steps: int = 8,
    decay: float = 0.6,
) -> list[int]:
    """Build integer wheel deltas that geometrically decay and (approximately) sum to ``total_px``.

    Models scroll-momentum bleed-off: the first wheel event is the largest and each
    subsequent event is ``decay`` times the previous (``v_n = v0 * decay**n``), with
    mild per-step jitter so the steps are not perfectly geometric. The returned deltas:

    - PRESERVE SIGN: a negative ``total_px`` (scroll up) yields all-negative deltas.
    - Sum to approximately ``total_px`` (the final step absorbs rounding remainder).
    - Avoid a zero-length runaway: steps that would round below 1px terminate the burst
      rather than emitting an unbounded tail of zeros.

    The base ``v0`` is chosen from the closed-form geometric sum so the un-jittered
    series would sum exactly to the magnitude:

        v0 = magnitude * (1 - decay) / (1 - decay**N)
    """
    sign = -1 if total_px < 0 else 1
    magnitude = abs(float(total_px))
    steps = int(max_steps)
    if magnitude < 1.0 or steps <= 0:
        return []

    d = min(max(float(decay), 0.05), 0.95)
    n = max(1, steps)

    denom = 1.0 - (d ** n)
    v0 = magnitude * (1.0 - d) / denom if denom > 1e-9 else magnitude

    deltas: list[int] = []
    remaining = magnitude
    for i in range(n):
        base = v0 * (d ** i)
        step = base * rng.uniform(0.85, 1.15)
        if step > remaining:
            step = remaining
        step_int = int(round(step))
        if step_int < 1:
            # Below 1px: don't emit a tail of zeros. Flush a meaningful remainder once.
            if remaining >= 1.0:
                step_int = int(round(remaining))
            else:
                break
        deltas.append(sign * step_int)
        remaining -= step_int
        if remaining < 1.0:
            break

    # Absorb any leftover into the last emitted step so the burst sums close to total.
    if deltas and remaining >= 1.0:
        deltas[-1] += sign * int(round(remaining))

    return deltas
