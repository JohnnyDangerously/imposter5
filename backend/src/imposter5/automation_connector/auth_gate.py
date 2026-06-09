"""Pre-run credential gate (pipeline seam — workstream B fills in the real logic).

Contract used by the ``/api/imposter5/run`` pipeline:

    decision = evaluate_auth(provider=..., url=..., prompt=..., goal=...)

``decision`` is an :class:`AuthDecision`. The run pipeline reads it BEFORE opening
the browser:

- ``required is False``  -> proceed normally (no credentials needed).
- ``required and ready`` -> proceed; the session already has usable credentials
  (e.g. stored cookies) so execution can run authenticated.
- ``required and not ready`` -> SHORT-CIRCUIT the run and return ``to_payload()`` to
  the UI so it can prompt the user for credentials (reusing the existing
  ``/api/imposter5/login/*`` endpoints), then the user re-runs.

This stub never blocks, so behavior is unchanged until workstream B implements:
- detecting auth-required targets (LinkedIn and other gated domains),
- checking the cookie/login store for readiness,
- producing a ``login_hint`` the frontend modal can act on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Providers that always require stored credentials before a run can succeed.
# LinkedIn is the only hard-gated provider today (its feed/profile pages redirect
# to a login wall when unauthenticated). Add new gated provider keys here; URL
# detection below maps gated hosts onto the same provider keys.
_GATED_PROVIDERS: frozenset[str] = frozenset({"linkedin"})

# Hosts that imply a gated provider even when ``provider`` was left generic.
_GATED_HOST_SUFFIXES: dict[str, str] = {"linkedin.com": "linkedin"}

# LinkedIn session/auth cookies. Presence of any one means the stored jar is an
# authenticated session rather than a pre-login anonymous jar.
_LINKEDIN_SESSION_COOKIES: frozenset[str] = frozenset({"li_at", "liap", "li_rm"})

# The /api/imposter5/run LinkedIn path scrapes under this fixed user_id, so the
# gate must check readiness against the same credential store the run will use.
RUN_USER_ID = "visible_watch_session"


@dataclass
class AuthDecision:
    """Result of the pre-run credential gate."""

    required: bool = False
    ready: bool = False
    provider: str = "generic"
    reason: str = ""
    # Frontend hint for collecting credentials (login URL, provider id, cookie key,
    # whether a headed manual-login window is needed, etc.). None when not required.
    login_hint: dict[str, Any] | None = None

    @property
    def blocks_run(self) -> bool:
        """True when the run must stop and ask the user for credentials first."""
        return self.required and not self.ready

    def to_payload(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "ready": self.ready,
            "provider": self.provider,
            "reason": self.reason,
            "login_hint": self.login_hint,
            "blocks_run": self.blocks_run,
        }


def _host(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.netloc or "").lower()


def _gated_provider(provider: str, url: str) -> str | None:
    """Return the gated provider key for this turn, or None when no auth is needed.

    A turn is gated when the explicit ``provider`` is a known gated provider, or
    when the target URL's host maps onto one (e.g. a linkedin.com link submitted
    with the generic provider).
    """
    key = (provider or "").strip().lower()
    if key in _GATED_PROVIDERS:
        return key
    host = _host(url)
    for suffix, mapped in _GATED_HOST_SUFFIXES.items():
        if host == suffix or host.endswith(f".{suffix}"):
            return mapped
    return None


def _stored_cookies(user_id: str, url: str) -> list[dict]:
    """Load the stored cookie jar for this user/target via the existing login store.

    Never raises: a missing store or unreachable S3 means "no usable credentials".
    """
    try:
        from imposter5.automation_connector import login_manager

        cookies = login_manager.load_site_cookies(user_id, url)
    except Exception as exc:  # pragma: no cover - defensive; loader already guards
        logger.info("[auth_gate] cookie load failed for readiness check: %s", exc)
        return []
    return cookies if isinstance(cookies, list) else []


def _credentials_ready(provider: str, cookies: list[dict]) -> bool:
    """True when the stored cookie jar represents a usable authenticated session."""
    if not cookies:
        return False
    if provider != "linkedin":
        # Other gated providers: any stored cookies count as usable.
        return True
    names = {
        str(cookie.get("name", "")).strip().lower()
        for cookie in cookies
        if isinstance(cookie, dict)
    }
    names.discard("")
    if not names:
        # Opaque jar (no inspectable names) — trust presence.
        return True
    return bool(names & _LINKEDIN_SESSION_COOKIES)


def _login_url_for(provider: str, url: str) -> str:
    if provider == "linkedin":
        return "https://www.linkedin.com/login"
    return (url or "").strip()


def _cookie_storage_key(user_id: str, url: str) -> str:
    """Best-effort storage key (informational hint for the frontend/debugging)."""
    try:
        from imposter5.automation_connector.login_manager import get_domain_clean

        domain = get_domain_clean(url)
    except Exception:  # pragma: no cover - defensive
        domain = "generic"
    return f"tokyo/user-data/prod/{domain}/cookies/{user_id}.json"


def _login_hint(provider: str, url: str, user_id: str) -> dict[str, Any]:
    """What the frontend modal needs to drive the EXISTING login flow + re-run.

    Reuses the existing ``/api/imposter5/login/*`` endpoints and credential store;
    no new auth backend is introduced.
    """
    login_url = _login_url_for(provider, url)
    return {
        "provider": provider,
        "user_id": user_id,
        "login_url": login_url,
        # The cookie name the run needs to consider the session authenticated.
        "cookie_name": "li_at" if provider == "linkedin" else None,
        "cookie_key": _cookie_storage_key(user_id, url),
        # Existing backend login flow the modal should call, in order.
        "flow": [
            {
                "step": "start",
                "endpoint": "/api/imposter5/login/start",
                "method": "POST",
                "body": {"user_id": user_id, "url": login_url},
                "description": "Opens a backend login browser window for the user to sign in.",
            },
            {
                "step": "verify",
                "endpoint": "/api/imposter5/login/verify",
                "method": "POST",
                "body": {"user_id": user_id, "url": login_url},
                "description": "Verifies the session and persists cookies once login completes.",
            },
        ],
    }


def evaluate_auth(
    *,
    provider: str,
    url: str,
    prompt: str | None = None,
    goal: Any = None,
    user_id: str = RUN_USER_ID,
) -> AuthDecision:
    """Decide whether a run needs credentials and whether they are ready.

    - Non-gated targets (generic web) never require credentials.
    - Gated targets (LinkedIn today) require credentials. They are ``ready`` when a
      usable stored cookie jar exists for ``user_id`` in the existing login store,
      and not ready (``blocks_run``) otherwise so the run short-circuits and the UI
      can collect credentials via the existing ``/api/imposter5/login/*`` flow.

    ``user_id`` defaults to the user the run pipeline scrapes under, so the gate
    checks the same credential store the run will actually consume.
    """
    gated_provider = _gated_provider(provider, url)
    if gated_provider is None:
        return AuthDecision(
            required=False,
            ready=True,
            provider=provider or "generic",
            reason="",
            login_hint=None,
        )

    cookies = _stored_cookies(user_id, url)
    ready = _credentials_ready(gated_provider, cookies)
    if ready:
        return AuthDecision(
            required=True,
            ready=True,
            provider=gated_provider,
            reason=f"Stored {gated_provider} credentials found; running authenticated.",
            login_hint=None,
        )

    return AuthDecision(
        required=True,
        ready=False,
        provider=gated_provider,
        reason=(
            f"{gated_provider} requires sign-in and no usable stored credentials "
            f"were found for this session."
        ),
        login_hint=_login_hint(gated_provider, url, user_id),
    )
