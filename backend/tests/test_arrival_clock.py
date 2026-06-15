"""Multi-scale tests for human-plausible campaign arrival timing (workstream D).

These guard the property that motivated the work: the deployed stream must look
human at *every* scale, not just per-gap. Each metric below is the same one a
sophisticated detector would compute, so passing them is evidence the stream
defeats the fixed-period / minute-grid / no-circadian fingerprint the blue team
caught — without merely papering over it with local jitter.

Thresholds carry comfortable margins to the observed range across seeds (see the
diagnostics CLI) so the suite is robust, not fit to one lucky stream.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from imposter5.automation_connector import arrival_clock as ac
from imposter5.automation_connector import arrival_diagnostics as ad
from imposter5.automation_connector.arrival_clock import (
    ArrivalState,
    advance,
    generate_stream,
    profile_for_persona,
)

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
PROFILE = profile_for_persona("focused_power_user")  # nine_to_five, America/New_York
SEEDS = ["alpha", "bravo", "charlie", "delta", "echo"]
DAYS = 60
INTERVAL = 120
WINDOWS = [3600.0, 6 * 3600.0, 24 * 3600.0, 3 * 24 * 3600.0]
SMALL_W, BIG_W = 3600.0, 3 * 24 * 3600.0


@pytest.fixture(scope="module")
def streams() -> dict[str, list[datetime]]:
    return {
        seed: generate_stream(
            start=START, days=DAYS, interval_minutes=INTERVAL, profile=PROFILE, seed=seed
        )
        for seed in SEEDS
    }


@pytest.fixture(scope="module")
def fixed_stream() -> list[datetime]:
    return ad._fixed_period_stream(start=START, days=DAYS, interval_minutes=INTERVAL)


# --------------------------------------------------------------------------- #
# Determinism / online-step contract
# --------------------------------------------------------------------------- #
def test_advance_is_deterministic_per_seed_and_step():
    state = ArrivalState.initial("seed-x")
    a1, s1 = advance(state, now=START, interval_minutes=INTERVAL, profile=PROFILE)
    a2, s2 = advance(state, now=START, interval_minutes=INTERVAL, profile=PROFILE)
    assert a1 == a2
    assert s1 == s2
    assert s1.step == state.step + 1


def test_generate_stream_reproducible_and_seed_independent():
    a = generate_stream(start=START, days=20, interval_minutes=INTERVAL, profile=PROFILE, seed="same")
    b = generate_stream(start=START, days=20, interval_minutes=INTERVAL, profile=PROFILE, seed="same")
    c = generate_stream(start=START, days=20, interval_minutes=INTERVAL, profile=PROFILE, seed="diff")
    assert a == b
    assert a != c


@pytest.mark.parametrize("seed", SEEDS)
def test_arrivals_are_strictly_increasing_and_off_grid(streams, seed):
    stream = streams[seed]
    assert stream == sorted(stream)
    assert all(b > a for a, b in zip(stream, stream[1:]))
    assert all(t.microsecond != 0 for t in stream)


def test_state_round_trips_through_json():
    state = ArrivalState(seed="s", step=4, log_rate=0.37, burst_remaining=2)
    assert ArrivalState.from_dict(state.to_dict()) == state
    assert ArrivalState.from_dict(None).seed  # fresh seed when missing
    assert ArrivalState.from_dict({}).seed


# --------------------------------------------------------------------------- #
# Zoomed-out structure (the whole point)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", SEEDS)
def test_fano_factor_rises_with_window(streams, seed):
    """Over-dispersion: counts must keep spreading as the window grows, unlike a
    flat renewal process whose Fano plateaus near the gaps' CV^2."""
    fano = ad.fano_factor_curve(streams[seed], WINDOWS)
    assert fano[BIG_W] > fano[SMALL_W] + 1.0
    assert fano[BIG_W] > 1.5  # clearly over-dispersed (observed 3.5-4.4)


def test_fixed_period_counts_do_not_over_disperse(fixed_stream):
    """The old behavior: equal windows hold near-equal counts, so Fano collapses
    toward zero at large scales — the opposite of human."""
    fano = ad.fano_factor_curve(fixed_stream, WINDOWS)
    assert fano[BIG_W] < 0.1


@pytest.mark.parametrize("seed", SEEDS)
def test_long_range_dependence(streams, seed, fixed_stream):
    """Hurst (DFA) > 0.5 means long-memory structure; iid/fixed sit near 0.5."""
    h_human = ad.hurst_dfa(streams[seed])
    h_fixed = ad.hurst_dfa(fixed_stream)
    assert h_human > 0.65  # observed 0.73-0.79
    assert h_human > h_fixed + 0.1


def test_no_dominant_cadence_spike_vs_fixed(streams, fixed_stream):
    """A fixed cadence is a sharp spectral line; the human stream is broadband
    (its only real periodicity is the legitimate 24h circadian rhythm)."""
    ps_human = ad.dominant_period_strength(streams["alpha"])
    ps_fixed = ad.dominant_period_strength(fixed_stream)
    assert ps_fixed > 300
    assert ps_human < 200  # observed 30-74
    assert ps_human < ps_fixed * 0.4


@pytest.mark.parametrize("seed", SEEDS)
def test_bursty_and_heavy_tailed(streams, seed):
    """Positive burstiness + lag-1 memory + CV>1: clustered, heavy-tailed gaps,
    not the perfectly regular (burstiness=-1, CV=0) fixed stream."""
    bm = ad.burstiness_memory(streams[seed])
    assert bm.cv > 0.9          # observed 1.3-1.8
    assert bm.burstiness > 0.05  # observed 0.13-0.29
    assert bm.memory > 0.02      # observed 0.06-0.20


# --------------------------------------------------------------------------- #
# Zoomed-in marginal still clean
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", SEEDS)
def test_marginal_gaps_fit_lognormal(streams, seed):
    assert ad.gap_ks_lognormal(streams[seed]) < 0.10  # observed ~0.05-0.06


# --------------------------------------------------------------------------- #
# Circadian + grid + fleet independence
# --------------------------------------------------------------------------- #
def test_circadian_keeps_overnight_quiet(streams):
    """A nine-to-five identity is nearly silent 00:00-06:00 local."""
    hist = ad.hour_histogram(streams["alpha"], tz=PROFILE.tz)
    night = hist[0:6].sum()
    midday = hist[9:18].sum()
    total = hist.sum()
    assert midday > 8 * night
    assert night / total < 0.08  # observed ~0.024


def test_off_grid_unlike_fixed(streams, fixed_stream):
    assert ad.grid_fraction(streams["alpha"]) == 0.0
    assert ad.grid_fraction(fixed_stream) == 1.0


def test_distinct_identities_do_not_fire_in_lockstep():
    """Same persona + cadence + start, different seeds => independent phase, so
    a fleet does not cluster on identical instants (a cross-session tell)."""
    a = generate_stream(start=START, days=DAYS, interval_minutes=INTERVAL, profile=PROFILE, seed="ident-A")
    b = generate_stream(start=START, days=DAYS, interval_minutes=INTERVAL, profile=PROFILE, seed="ident-B")
    assert a[0] != b[0]
    assert abs(ad.cross_correlation(a, b, bin_s=600.0)) < 0.25  # observed ~-0.01


# --------------------------------------------------------------------------- #
# Persona -> circadian mapping
# --------------------------------------------------------------------------- #
def test_profile_for_persona_mapping():
    assert profile_for_persona("focused_power_user").chronotype == "nine_to_five"
    assert profile_for_persona("late_day_review").chronotype == "night_owl"
    assert profile_for_persona("curious_reader").chronotype == "evening"
    assert profile_for_persona("mobile_checker").chronotype == "early_bird"
    assert profile_for_persona("unknown-persona").chronotype == "nine_to_five"
    assert profile_for_persona(None).chronotype == "nine_to_five"
    assert profile_for_persona("x", timezone="Europe/London").timezone == "Europe/London"
