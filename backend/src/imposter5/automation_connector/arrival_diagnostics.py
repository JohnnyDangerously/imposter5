"""Multi-scale diagnostics for arrival-time streams (the math-quality gate).

Per-event jitter only fixes the *marginal* gap distribution. These metrics
measure structure at every scale, so we can prove an arrival stream is human-
plausible "zoomed out" and not merely "zoomed in":

- :func:`fano_factor_curve` — Var/Mean of event counts vs window size. A flat
  renewal process plateaus near the gaps' CV^2; over-dispersed (human) streams
  keep rising with the window.
- :func:`dominant_period_strength` — peak-to-mean of the count spectrum. A
  fixed-period scheduler shows a sharp spike; a human stream does not.
- :func:`hurst_dfa` — detrended fluctuation exponent. ~0.5 for white/iid,
  > 0.5 for long-range dependence (drift / burstiness).
- :func:`burstiness_memory` — Goh-Barabasi burstiness B and lag-1 gap memory M.
- :func:`gap_ks_lognormal` — one-sample KS of the marginal gaps vs a fitted
  lognormal (the zoomed-in check), implemented without scipy.
- :func:`grid_fraction` — share of timestamps on the whole-second grid.
- :func:`cross_correlation` — activity correlation between two identities.

Used by tests and as a CLI report. numpy-only (a declared dependency); scipy is
deliberately avoided because it is not in the project's dependency set.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import numpy as np


def _epoch_seconds(times: Sequence[datetime]) -> np.ndarray:
    """Sorted seconds-since-first, microsecond precise."""
    secs = np.array(
        [t.astimezone(timezone.utc).timestamp() for t in times], dtype=float
    )
    secs.sort()
    return secs - secs[0] if secs.size else secs


def fano_factor_curve(
    times: Sequence[datetime], windows_s: Sequence[float]
) -> dict[float, float]:
    """Fano factor F(T) = Var(N)/E(N) of counts in non-overlapping windows."""
    secs = _epoch_seconds(times)
    if secs.size < 4:
        return {float(w): float("nan") for w in windows_s}
    span = float(secs[-1])
    out: dict[float, float] = {}
    for window in windows_s:
        window = float(window)
        nbins = int(span // window)
        if nbins < 4:
            out[window] = float("nan")
            continue
        edges = np.arange(nbins + 1) * window
        counts = np.histogram(secs, bins=edges)[0].astype(float)
        mean = counts.mean()
        out[window] = float(counts.var() / mean) if mean > 0 else float("nan")
    return out


def _count_signal(secs: np.ndarray, bin_s: float) -> np.ndarray:
    span = float(secs[-1]) if secs.size else 0.0
    nbins = max(8, int(span // bin_s))
    edges = np.linspace(0.0, span, nbins + 1)
    return np.histogram(secs, bins=edges)[0].astype(float)


def dominant_period_strength(times: Sequence[datetime], bin_s: float = 600.0) -> float:
    """Peak-to-mean ratio of the count power spectrum (DC excluded).

    High => a dominant period (the fixed-interval fingerprint). ~1-ish => no
    single period stands out.
    """
    secs = _epoch_seconds(times)
    if secs.size < 8:
        return float("nan")
    signal = _count_signal(secs, bin_s)
    signal = signal - signal.mean()
    if not np.any(signal):
        return float("nan")
    power = np.abs(np.fft.rfft(signal)) ** 2
    power = power[1:]  # drop DC
    if power.size == 0 or power.mean() == 0:
        return float("nan")
    return float(power.max() / power.mean())


def hurst_dfa(times: Sequence[datetime], bin_s: float = 1800.0) -> float:
    """Detrended Fluctuation Analysis exponent of the binned count series."""
    secs = _epoch_seconds(times)
    if secs.size < 16:
        return float("nan")
    x = _count_signal(secs, bin_s)
    n = x.size
    if n < 16:
        return float("nan")
    y = np.cumsum(x - x.mean())
    scales = np.unique(np.floor(np.logspace(np.log10(4), np.log10(n // 4), 12)).astype(int))
    scales = scales[scales >= 4]
    fluct: list[float] = []
    used: list[int] = []
    for s in scales:
        nseg = n // s
        if nseg < 1:
            continue
        t = np.arange(s)
        rms = []
        for v in range(nseg):
            seg = y[v * s : (v + 1) * s]
            coef = np.polyfit(t, seg, 1)
            resid = seg - np.polyval(coef, t)
            rms.append(np.sqrt(np.mean(resid**2)))
        mean_rms = float(np.mean(rms))
        if mean_rms > 0:
            fluct.append(mean_rms)
            used.append(int(s))
    if len(used) < 3:
        return float("nan")
    slope = np.polyfit(np.log(used), np.log(fluct), 1)[0]
    return float(slope)


@dataclass(frozen=True)
class BurstMemory:
    burstiness: float  # B in [-1, 1]; >0 is bursty, 0 is Poisson, <0 is regular
    memory: float      # lag-1 autocorrelation of consecutive gaps
    cv: float          # coefficient of variation of gaps


def burstiness_memory(times: Sequence[datetime]) -> BurstMemory:
    secs = _epoch_seconds(times)
    gaps = np.diff(secs)
    if gaps.size < 3:
        return BurstMemory(float("nan"), float("nan"), float("nan"))
    mean = float(gaps.mean())
    std = float(gaps.std())
    burst = (std - mean) / (std + mean) if (std + mean) > 0 else float("nan")
    a, b = gaps[:-1], gaps[1:]
    if a.std() > 0 and b.std() > 0:
        memory = float(np.corrcoef(a, b)[0, 1])
    else:
        memory = float("nan")
    cv = std / mean if mean > 0 else float("nan")
    return BurstMemory(burst, memory, cv)


def gap_ks_lognormal(times: Sequence[datetime]) -> float:
    """One-sample KS distance of gaps vs a lognormal fit by MLE (no scipy)."""
    secs = _epoch_seconds(times)
    gaps = np.diff(secs)
    gaps = gaps[gaps > 0]
    if gaps.size < 8:
        return float("nan")
    logs = np.log(gaps)
    mu, sigma = float(logs.mean()), float(logs.std())
    if sigma <= 0:
        return float("nan")
    ordered = np.sort(gaps)
    # Theoretical lognormal CDF via the standard-normal CDF (erf), no scipy.
    z = (np.log(ordered) - mu) / (sigma * np.sqrt(2.0))
    cdf = 0.5 * (1.0 + np.vectorize(_erf)(z))
    n = ordered.size
    emp_hi = np.arange(1, n + 1) / n
    emp_lo = np.arange(0, n) / n
    return float(np.max(np.maximum(np.abs(cdf - emp_hi), np.abs(cdf - emp_lo))))


def _erf(x: float) -> float:
    import math

    return math.erf(x)


def grid_fraction(times: Sequence[datetime]) -> float:
    """Fraction of timestamps that fall exactly on a whole second (grid tell)."""
    if not times:
        return float("nan")
    on_grid = sum(1 for t in times if t.microsecond == 0)
    return on_grid / len(times)


def hour_histogram(times: Sequence[datetime], tz: timezone | None = None) -> np.ndarray:
    """Counts per local hour-of-day (0..23). Defaults to UTC if no tz given."""
    hist = np.zeros(24, dtype=float)
    for t in times:
        local = t.astimezone(tz) if tz is not None else t.astimezone(timezone.utc)
        hist[local.hour] += 1
    return hist


def cross_correlation(
    a: Sequence[datetime], b: Sequence[datetime], bin_s: float = 3600.0
) -> float:
    """Pearson correlation of two identities' hourly activity over a shared span."""
    if not a or not b:
        return float("nan")
    sa = np.array([t.astimezone(timezone.utc).timestamp() for t in a], dtype=float)
    sb = np.array([t.astimezone(timezone.utc).timestamp() for t in b], dtype=float)
    lo, hi = min(sa.min(), sb.min()), max(sa.max(), sb.max())
    nbins = max(8, int((hi - lo) // bin_s))
    edges = np.linspace(lo, hi, nbins + 1)
    ca = np.histogram(sa, bins=edges)[0].astype(float)
    cb = np.histogram(sb, bins=edges)[0].astype(float)
    if ca.std() == 0 or cb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ca, cb)[0, 1])


def report(times: Sequence[datetime], windows_s: Sequence[float] | None = None) -> dict:
    """Bundle the full multi-scale picture for a stream."""
    windows_s = windows_s or [3600.0, 6 * 3600.0, 24 * 3600.0, 3 * 24 * 3600.0]
    bm = burstiness_memory(times)
    return {
        "n_events": len(times),
        "fano": fano_factor_curve(times, windows_s),
        "period_strength": dominant_period_strength(times),
        "hurst_dfa": hurst_dfa(times),
        "burstiness": bm.burstiness,
        "memory": bm.memory,
        "cv": bm.cv,
        "gap_ks_lognormal": gap_ks_lognormal(times),
        "grid_fraction": grid_fraction(times),
    }


def _fixed_period_stream(
    *, start: datetime, days: float, interval_minutes: float
) -> list[datetime]:
    """The OLD scheduler's behavior: exactly periodic, on the whole-minute grid.

    Provided so callers/tests have the adversarial baseline to beat.
    """
    from datetime import timedelta

    out: list[datetime] = []
    step = timedelta(minutes=interval_minutes)
    t = start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    end = start + timedelta(days=days)
    while t < end:
        out.append(t)
        t = t + step
    return out


if __name__ == "__main__":  # pragma: no cover - convenience CLI
    import json
    from datetime import datetime as _dt

    from imposter5.automation_connector.arrival_clock import (
        generate_stream,
        profile_for_persona,
    )

    start = _dt(2026, 6, 1, tzinfo=timezone.utc)
    profile = profile_for_persona("focused_power_user")
    human = generate_stream(
        start=start, days=45, interval_minutes=120, profile=profile, seed="cli-demo"
    )
    fixed = _fixed_period_stream(start=start, days=45, interval_minutes=120)
    print("human :", json.dumps(report(human), indent=2, default=str))
    print("fixed :", json.dumps(report(fixed), indent=2, default=str))
