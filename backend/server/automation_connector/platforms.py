"""URL/provider helpers for the portable automation connector."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

DEFAULT_AUTOMATION_URL = ""


def safe_string(value: Any) -> str:
    """Return a trimmed string for loose JSON/API values."""
    return str(value or "").strip()


def display_url(value: str) -> str:
    """Normalize a user-entered website into a navigable URL."""
    raw = safe_string(value) or DEFAULT_AUTOMATION_URL
    if not raw:
        return ""
    return raw if "://" in raw else f"https://{raw}"


def platform_for_url(url: str) -> str:
    """Map a URL to a provider key without exposing product-specific routing."""
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return "linkedin"
    return "generic_web"


def entity_type_for_platform(platform: str) -> str:
    """Return the monitored-target entity type for a provider platform."""
    if platform == "linkedin":
        return "linkedin_profile"
    return "generic_web"


def identity_key(entity_int_id: int, website_url: str) -> str:
    """Build the owner-scoped provider identity key."""
    platform = platform_for_url(website_url)
    return f"{platform}:{entity_int_id}"
