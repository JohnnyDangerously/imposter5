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

from dataclasses import dataclass, field
from typing import Any


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


def evaluate_auth(
    *,
    provider: str,
    url: str,
    prompt: str | None = None,
    goal: Any = None,
) -> AuthDecision:
    """Decide whether a run needs credentials and whether they are ready.

    STUB: currently always permissive (never blocks) so the run pipeline behaves
    exactly as before. Workstream B replaces the body with real detection +
    readiness checks against the login/cookie store.
    """
    return AuthDecision(required=False, ready=True, provider=provider, reason="", login_hint=None)
