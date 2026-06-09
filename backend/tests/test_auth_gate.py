"""Tests for the pre-run credential gate (workstream B).

Covers the three contract cases the /api/imposter5/run pipeline depends on:
- LinkedIn without usable creds -> required, not ready, blocks_run.
- LinkedIn with stored session creds -> required, ready, does not block.
- Non-gated generic URL -> not required (never blocks).

Readiness reads the existing login/cookie store via login_manager.load_site_cookies,
which is monkeypatched here so the suite never touches local files or S3.
"""
from __future__ import annotations

import pytest

from imposter5.automation_connector import auth_gate
from imposter5.automation_connector import login_manager


@pytest.fixture()
def stub_cookies(monkeypatch):
    """Patch the cookie store the gate reads; returns a setter for the jar."""

    jar: dict[str, list[dict]] = {"cookies": []}

    def fake_load(user_id: str, url: str) -> list[dict]:
        return jar["cookies"]

    monkeypatch.setattr(login_manager, "load_site_cookies", fake_load)

    def _set(cookies: list[dict]) -> None:
        jar["cookies"] = cookies

    return _set


def test_linkedin_without_creds_blocks_run(stub_cookies):
    stub_cookies([])

    decision = auth_gate.evaluate_auth(
        provider="linkedin",
        url="https://www.linkedin.com/feed/",
        prompt=None,
        goal=None,
    )

    assert decision.required is True
    assert decision.ready is False
    assert decision.blocks_run is True
    assert decision.provider == "linkedin"
    # login_hint must drive the EXISTING login flow.
    assert decision.login_hint is not None
    endpoints = {step["endpoint"] for step in decision.login_hint["flow"]}
    assert "/api/imposter5/login/start" in endpoints
    assert "/api/imposter5/login/verify" in endpoints
    assert decision.login_hint["provider"] == "linkedin"
    assert decision.login_hint["cookie_name"] == "li_at"
    # Payload round-trips for the API response.
    payload = decision.to_payload()
    assert payload["blocks_run"] is True
    assert payload["login_hint"]["login_url"]


def test_linkedin_with_stored_creds_is_ready(stub_cookies):
    stub_cookies([{"name": "li_at", "value": "session-token", "domain": ".linkedin.com"}])

    decision = auth_gate.evaluate_auth(
        provider="linkedin",
        url="https://www.linkedin.com/feed/",
        prompt=None,
        goal=None,
    )

    assert decision.required is True
    assert decision.ready is True
    assert decision.blocks_run is False
    assert decision.login_hint is None


def test_linkedin_detected_from_url_with_generic_provider(stub_cookies):
    stub_cookies([])

    decision = auth_gate.evaluate_auth(
        provider="generic",
        url="https://www.linkedin.com/in/someone",
        prompt=None,
        goal=None,
    )

    assert decision.required is True
    assert decision.ready is False
    assert decision.blocks_run is True
    assert decision.provider == "linkedin"


def test_linkedin_jar_without_session_cookie_is_not_ready(stub_cookies):
    # Cookies present but none of them are an authenticated-session cookie.
    stub_cookies([{"name": "bcookie", "value": "anon"}, {"name": "lang", "value": "en"}])

    decision = auth_gate.evaluate_auth(
        provider="linkedin",
        url="https://www.linkedin.com/feed/",
    )

    assert decision.required is True
    assert decision.ready is False
    assert decision.blocks_run is True


def test_generic_url_not_required(stub_cookies):
    stub_cookies([])

    decision = auth_gate.evaluate_auth(
        provider="generic",
        url="https://en.wikipedia.org/wiki/Artificial_intelligence",
        prompt="read the page",
        goal=None,
    )

    assert decision.required is False
    assert decision.ready is True
    assert decision.blocks_run is False
    assert decision.login_hint is None


def test_generic_url_not_required_even_without_cookie_store():
    # No monkeypatch: a generic target must short-circuit before touching the store.
    decision = auth_gate.evaluate_auth(
        provider="generic",
        url="https://news.ycombinator.com",
    )

    assert decision.required is False
    assert decision.blocks_run is False
