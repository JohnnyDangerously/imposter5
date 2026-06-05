"""CloakBrowser session manager for LinkedIn.

Handles:
- CloakBrowser install / import guard
- Per-user cookie persistence in S3 (tokyo/user-data/prod/linkedin/cookies/{user_id}.json)
- Launch a stealth Chromium page, restore cookies, and navigate to a URL

Usage::

    from loaders.linkedin_browser import LinkedInBrowserSession

    with LinkedInBrowserSession(user_id="12345") as page:
        page.goto("https://www.linkedin.com/feed/")
        html = page.content()
"""

from __future__ import annotations

import json
import logging
import os
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# S3 cookie storage
# ---------------------------------------------------------------------------

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
_COOKIE_BUCKET = os.environ.get("EMAIL_SIGNALS_BUCKET", "via-internal-apps")
_COOKIE_PREFIX = "tokyo/user-data/prod/linkedin/cookies"
_PROFILE_ROOT = os.environ.get("AUTOMATION_CONNECTOR_PROFILE_DIR", "/tmp/tokyo-automation-profiles")
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _log_user_id(user_id: str) -> str:
    return sha256(str(user_id or "").encode()).hexdigest()[:12]


def linkedin_profile_dir(user_id: str) -> str:
    """Return the persistent CloakBrowser profile directory for a LinkedIn user."""
    safe_user_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(user_id or "unknown"))
    return str(Path(_PROFILE_ROOT) / "linkedin" / safe_user_id)


def _s3() -> Any:
    return boto3.client("s3", region_name=AWS_REGION)


def _cookie_key(user_id: str) -> str:
    return f"{_COOKIE_PREFIX}/{user_id}.json"


def load_cookies(user_id: str) -> list[dict]:
    """Load LinkedIn cookies for *user_id* from S3. Returns [] if not found."""
    key = _cookie_key(user_id)
    user_hash = _log_user_id(user_id)
    try:
        resp = _s3().get_object(Bucket=_COOKIE_BUCKET, Key=key)
        data = json.loads(resp["Body"].read())
        logger.info("[linkedin_browser] loaded %d cookies for user_hash %s", len(data), user_hash)
        return data if isinstance(data, list) else []
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            logger.info("[linkedin_browser] no saved cookies for user_hash %s", user_hash)
            return []
        logger.warning("[linkedin_browser] S3 load error for user_hash %s: %s", user_hash, exc)
        return []


def save_cookies(user_id: str, cookies: list[dict]) -> None:
    """Persist *cookies* list to S3 for *user_id*."""
    key = _cookie_key(user_id)
    user_hash = _log_user_id(user_id)
    try:
        _s3().put_object(
            Bucket=_COOKIE_BUCKET,
            Key=key,
            Body=json.dumps(cookies, ensure_ascii=False).encode(),
            ContentType="application/json",
        )
        logger.info("[linkedin_browser] saved %d cookies for user_hash %s", len(cookies), user_hash)
    except Exception as exc:
        logger.error("[linkedin_browser] S3 save error for user_hash %s: %s", user_hash, exc)


# ---------------------------------------------------------------------------
# CloakBrowser session
# ---------------------------------------------------------------------------


class LinkedInBrowserSession:
    """Context manager that opens a CloakBrowser page with saved LinkedIn cookies.

    Parameters
    ----------
    user_id:
        Canonical user identifier used as the S3 cookie key.
    headless:
        Whether to run headless (default True).
    timeout_ms:
        Default navigation timeout in milliseconds.
    """

    def __init__(
        self,
        user_id: str,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        record_video_dir: str | None = None,
    ) -> None:
        """Configure the persistent browser session for one LinkedIn user.

        record_video_dir: if provided, passed to the persistent context launch so
        Playwright will record a video (.webm) of the session (including any
        injected synthetic cursor overlay for visual judgment of mouse moves).
        """
        self.user_id = user_id
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.record_video_dir = record_video_dir
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    def __enter__(self) -> Any:
        """Launch CloakBrowser, restore cookies, and return the page."""
        # Use the centralized launch_automation_persistent_context so all Cloak hookups
        # (including humanize + human_config for mechanics quality) are behind one
        # maintainable interface in cloak_runtime. This file owns LinkedIn cookie/profile
        # specifics; the launch surface is delegated.
        from imposter5.loaders.cloak_runtime import (
            apply_anti_fingerprint_init_script,
            automation_connector_stealth_context_kwargs,
            launch_automation_persistent_context,
        )

        ctx_kwargs = automation_connector_stealth_context_kwargs()
        # Preserve the historical _USER_AGENT if someone overrode it, but prefer the stealth helper.
        if _USER_AGENT and _USER_AGENT != ctx_kwargs.get("user_agent"):
            ctx_kwargs = {**ctx_kwargs, "user_agent": _USER_AGENT}

        if self.record_video_dir:
            ctx_kwargs = {**ctx_kwargs, "record_video_dir": self.record_video_dir}
            ctx_kwargs.setdefault("record_video_size", {"width": 1440, "height": 900})

        self._context = launch_automation_persistent_context(
            linkedin_profile_dir(self.user_id),
            headless=self.headless,
            **ctx_kwargs,
        )
        try:
            apply_anti_fingerprint_init_script(self._context)
        except Exception:
            pass
        self._context.set_default_timeout(self.timeout_ms)

        # Restore saved session cookies.
        cookies = load_cookies(self.user_id)
        if cookies:
            try:
                self._context.add_cookies(cookies)
            except Exception as exc:
                logger.warning(
                    "[linkedin_browser] failed to restore cookies for user_hash %s: %s",
                    _log_user_id(self.user_id),
                    exc,
                )

        pages = getattr(self._context, "pages", None) or []
        self._page = pages[0] if pages else self._context.new_page()
        return self._page

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Save cookies then tear down the browser."""
        if self._context is not None:
            try:
                cookies = self._context.cookies()
                if cookies:
                    save_cookies(self.user_id, cookies)
            except Exception as exc:
                logger.warning(
                    "[linkedin_browser] failed to capture cookies on exit for user_hash %s: %s",
                    _log_user_id(self.user_id),
                    exc,
                )
        for attr in ("_page", "_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass


def is_logged_in(page: Any) -> bool:
    """Return True if the current page indicates an active LinkedIn session."""
    try:
        url = page.url or ""
        if (
            "linkedin.com/login" in url
            or "linkedin.com/uas/login" in url
            or "checkpoint/challenge" in url
        ):
            return False
        selectors = [
            "[data-test-global-nav-link], nav.global-nav, .global-nav",
            (
                "input[placeholder*='Search'], input[placeholder*='looking for'], "
                "input[aria-label*='Search'], .search-global-typeahead__input"
            ),
            (
                "a[href*='/feed/'][aria-label*='Home'], a[href*='/mynetwork/'], "
                "a[href*='/jobs/'], a[href*='/messaging/'], a[href*='/notifications/']"
            ),
            "button[aria-label*='Me'], button[aria-label*='Account'], img.global-nav__me-photo",
        ]
        return any(page.query_selector(selector) is not None for selector in selectors)
    except Exception:
        return False
