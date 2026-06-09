"""Tests for the LinkedIn feed goal+Markov hybrid and the naive_bot removal.

These cover pure logic (no browser): the scan matrix invariants that keep the
Markov walk goal-free (no autonomous click/typing), the interest-term resolution
and scoring that drive the "open the one interesting post" behavior, and the
guarantee that the naive_bot test persona is gone from the product pool.
"""
from __future__ import annotations

from imposter5.automation_connector.behavior_policy import load_personas
from imposter5.loaders.linkedin_feed_scraper import (
    FEED_SCAN_MATRIX,
    _resolve_interest_terms,
    _score_post_interest,
)
from imposter5.loaders.markov_simulator import _normalize_matrix


# --- Scan matrix: Markov drives motion, the goal owns the clicks ------------- #
def test_feed_scan_matrix_rows_normalize() -> None:
    # Must be a valid transition matrix (every row sums to ~1.0).
    normalized = _normalize_matrix(FEED_SCAN_MATRIX, source="feed_scan_test")
    for row in normalized.values():
        assert abs(sum(row.values()) - 1.0) < 1e-6


def test_feed_scan_matrix_has_no_autonomous_click_or_typing() -> None:
    # The whole point of the hybrid: the random walk never fires an arbitrary
    # navigation. Only the goal layer opens a post.
    for state, row in FEED_SCAN_MATRIX.items():
        assert state not in ("click", "typing")
        assert "click" not in row
        assert "typing" not in row


# --- Interest term resolution ----------------------------------------------- #
def test_resolve_interest_terms_from_plan_and_variations() -> None:
    plan = {
        "interest_terms": "Founder, AI infra",
        "variations": {"icp_terms": ["Series A", "hiring eng"]},
    }
    terms = _resolve_interest_terms(plan)
    assert terms == ["founder", "ai infra", "series a", "hiring eng"]


def test_resolve_interest_terms_falls_back_to_defaults() -> None:
    # With nothing configured, the human-interest behavior stays alive on a
    # default run by falling back to the generic professional-interest vocabulary
    # (lowercased) rather than going dead.
    from imposter5.loaders.linkedin_feed_scraper import _DEFAULT_INTEREST_TERMS

    expected = [t.lower() for t in _DEFAULT_INTEREST_TERMS]
    assert _resolve_interest_terms({}) == expected
    assert _resolve_interest_terms(None) == expected


# --- Interest scoring -------------------------------------------------------- #
def test_term_match_outscores_substance_only() -> None:
    terms = ["ai infra"]
    matched = _score_post_interest("We are building AI infra for agents.", terms)
    long_irrelevant = _score_post_interest("x" * 4000, terms)
    assert matched > long_irrelevant


def test_empty_text_scores_zero() -> None:
    assert _score_post_interest("", ["anything"]) == 0.0


# --- naive_bot is gone from the product pool -------------------------------- #
def test_naive_bot_removed_from_personas() -> None:
    names = {p.name for p in load_personas()}
    assert "naive_bot" not in names
    assert names  # pool is non-empty
