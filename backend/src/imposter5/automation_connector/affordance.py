"""Affordance resolution for Red Team Automation profiles.

A site's *automation profile* tells the engine HOW to find the affordances a
feed-browse campaign acts on (feed posts, nav targets, search, results, profile
sections, like) — without hard-coding one site's element ids into the behavior
code. Resolution is a CASCADE so it works ~9/10 with no per-run finickiness: each
role is tried by several independent strategies and the first VISIBLE, non-honeypot
match wins. No single brittle selector can sink it.

Cascade (per role, in order):
  1. Profile CSS      — explicit selectors from the site profile (exact, fastest).
  2. Semantic CSS     — generic role/aria/landmark/href/data-* heuristics that hold
                        for "real" sites (LinkedIn is semantic, so this alone hits
                        most roles).
  3. Text / ARIA name — find a clickable by its accessible name ("Notifications"),
                        via get_by_role(link|button, name=...). Robust to class churn.
  4. Vision (pluggable) — a registered screenshot/vision resolver, used last when the
                        DOM yields nothing. Not implemented here; ``register_vision_resolver``
                        installs one. This is the safety net for the last ~10% /
                        non-semantic sites.

The profile lives on the website entry (websites.json -> ``automation_profile``);
built-in profiles ship for LinkedIn and the gauntlet, and an unknown site falls back
to the generic feed profile (semantic + text strategies only).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from imposter5.automation_connector.interaction_primitives import _detect_honeypot_reason

logger = logging.getLogger(__name__)

# Roles a feed-browse campaign resolves. Container/clickable roles are resolved to
# live elements; *field* roles (author/headline/text/actions row) are sub-selectors
# used inside a post element (and in the capture DOM read).
CLICKABLE_ROLES = (
    "nav_home", "nav_notifications", "nav_messages", "nav_network", "nav_jobs",
    "search_input", "search_submit", "result_name", "profile_section",
    "profile_back", "feed_post", "feed_like",
)
FIELD_ROLES = ("feed_author", "feed_headline", "feed_text", "feed_actions_row")


@dataclass(frozen=True)
class RoleHints:
    css: tuple[str, ...] = ()
    text: tuple[str, ...] = ()

    @classmethod
    def from_any(cls, value: Any) -> "RoleHints":
        if isinstance(value, str):
            return cls(css=(value,))
        if isinstance(value, (list, tuple)):
            return cls(css=tuple(str(v) for v in value))
        if isinstance(value, dict):
            css = value.get("css") or value.get("selectors") or ()
            text = value.get("text") or value.get("labels") or ()
            if isinstance(css, str):
                css = [css]
            if isinstance(text, str):
                text = [text]
            return cls(css=tuple(str(c) for c in css), text=tuple(str(t) for t in text))
        return cls()


@dataclass
class AutomationProfile:
    name: str = "generic"
    kind: str = "feed"
    roles: dict[str, RoleHints] = field(default_factory=dict)
    campaign: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AutomationProfile":
        if not isinstance(data, dict):
            return cls()
        roles_raw = data.get("roles") or {}
        roles = {str(r): RoleHints.from_any(h) for r, h in roles_raw.items()} if isinstance(roles_raw, dict) else {}
        return cls(
            name=str(data.get("name", "generic")),
            kind=str(data.get("kind", "feed")),
            roles=roles,
            campaign=dict(data.get("campaign") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "roles": {r: {"css": list(h.css), "text": list(h.text)} for r, h in self.roles.items()},
            "campaign": dict(self.campaign),
        }


# --- generic semantic + text fallbacks (apply to ANY site) ------------------- #
GENERIC_ROLE_CSS: dict[str, tuple[str, ...]] = {
    "feed_post": ("[role=feed] [role=article]", "[role=article]", "article", "[data-urn]", "main li"),
    "feed_like": ("button[aria-label*='like' i]", "[role=button][aria-label*='like' i]"),
    "nav_home": ("a[aria-label*='home' i]", "nav a[href$='/feed/']", "header a[aria-label*='home' i]"),
    "nav_notifications": ("a[href*='notification' i]", "[aria-label*='notification' i]", "[aria-label*='alert' i]"),
    "nav_messages": ("a[href*='messag' i]", "[aria-label*='messag' i]"),
    "nav_network": ("a[href*='mynetwork' i]", "a[href*='network' i]", "a[href*='connection' i]", "[aria-label*='network' i]"),
    "nav_jobs": ("a[href*='job' i]", "[aria-label*='job' i]"),
    "search_input": ("input[type=search]", "[role=searchbox]", "input[aria-label*='search' i]", "input[placeholder*='search' i]"),
    "search_submit": ("[role=search] button", "button[aria-label*='search' i]", "button[type=submit]"),
    "result_name": ("[role=listitem] a[href*='/in/']", "a[href*='/in/']", "[role=listitem] a[href]", "main li a[href]", "article a[href]"),
    "profile_section": ("section[aria-label]", "[data-section]", "main section", "section.artdeco-card"),
    "profile_back": ("button[aria-label*='back' i]", "a[aria-label*='back' i]", "[role=button][aria-label*='back' i]"),
}
GENERIC_FIELD_CSS: dict[str, tuple[str, ...]] = {
    "feed_author": (".update-components-actor__title", ".update-components-actor__name", ".name", "[class*='actor'] span[aria-hidden=true]"),
    "feed_headline": (".update-components-actor__description", ".meta", "[class*='actor__description']"),
    "feed_text": (".update-components-text", ".feed-shared-update-v2__description", ".text", "[class*='update-components-text']"),
    "feed_actions_row": (".feed-shared-social-action-bar", ".social-actions-buttons", ".actions", "[class*='social-action']"),
}
ROLE_DEFAULT_LABELS: dict[str, tuple[str, ...]] = {
    "nav_home": ("Home",),
    "nav_notifications": ("Notifications", "Alerts"),
    "nav_messages": ("Messaging", "Messages"),
    "nav_network": ("My Network", "Network", "Connections"),
    "nav_jobs": ("Jobs",),
    "feed_like": ("Like", "React Like"),
    "search_submit": ("Search",),
    "profile_back": ("Back",),
}

# --- pluggable vision strategy (the "by screenshot" safety net) -------------- #
# A vision resolver takes (page, role, profile) and returns a live locator/handle
# or None. Not implemented in this pass; install one with register_vision_resolver.
VisionResolver = Callable[[Any, str, "AutomationProfile"], Any]
_VISION_RESOLVER: VisionResolver | None = None


def register_vision_resolver(fn: VisionResolver | None) -> None:
    global _VISION_RESOLVER
    _VISION_RESOLVER = fn


class RoleResolver:
    """Resolve affordance roles on a live page via the cascade above."""

    def __init__(self, page: Any, profile: AutomationProfile | None = None) -> None:
        self.page = page
        self.profile = profile or AutomationProfile()

    # --- visibility / honeypot (same principle the SiteMapper uses) ----------- #
    def _is_usable(self, locator: Any) -> bool:
        try:
            if not locator.is_visible():
                return False
        except Exception:
            return False
        try:
            reason = _detect_honeypot_reason(locator)
        except Exception:
            return True
        if not reason:
            return True
        reasons = {r for r in reason.split(",") if r}
        reasons.discard("offscreen")  # below-the-fold is reachable by scrolling
        return not reasons

    def _usable_matches(self, selector: str, *, limit: int = 60) -> list[Any]:
        try:
            candidates = self.page.locator(selector).all()
        except Exception:
            return []
        out = []
        for loc in candidates[:limit]:
            if self._is_usable(loc):
                out.append(loc)
        return out

    def _css_candidates(self, role: str) -> list[str]:
        hints = self.profile.roles.get(role)
        out: list[str] = list(hints.css) if hints else []
        out.extend(GENERIC_ROLE_CSS.get(role, ()))
        # de-dup, keep order
        seen: set[str] = set()
        return [c for c in out if not (c in seen or seen.add(c))]

    def css_candidates(self, role: str) -> list[str]:
        """Public: ordered CSS candidates for a clickable role (profile + generic),
        for callers that scope the lookup inside another element."""
        return self._css_candidates(role)

    def _text_labels(self, role: str) -> list[str]:
        hints = self.profile.roles.get(role)
        out: list[str] = list(hints.text) if hints else []
        out.extend(ROLE_DEFAULT_LABELS.get(role, ()))
        seen: set[str] = set()
        return [t for t in out if not (t in seen or seen.add(t))]

    def _by_text(self, label: str) -> Any | None:
        """Find a clickable by accessible name (link first, then button)."""
        for getter in ("link", "button"):
            try:
                loc = self.page.get_by_role(getter, name=label, exact=False)
                matches = loc.all()
            except Exception:
                matches = []
            for m in matches[:20]:
                if self._is_usable(m):
                    return m
        # Last text resort: any element whose visible text matches.
        try:
            loc = self.page.get_by_text(label, exact=False)
            for m in loc.all()[:20]:
                if self._is_usable(m):
                    return m
        except Exception:
            pass
        return None

    # --- public API ----------------------------------------------------------- #
    def one(self, role: str) -> Any | None:
        for sel in self._css_candidates(role):
            matches = self._usable_matches(sel)
            if matches:
                return matches[0]
        for label in self._text_labels(role):
            hit = self._by_text(label)
            if hit is not None:
                return hit
        if _VISION_RESOLVER is not None:
            try:
                return _VISION_RESOLVER(self.page, role, self.profile)
            except Exception:
                logger.debug("[affordance] vision resolver failed for %s", role, exc_info=True)
        return None

    def all(self, role: str, *, limit: int = 60) -> list[Any]:
        for sel in self._css_candidates(role):
            matches = self._usable_matches(sel, limit=limit)
            if matches:
                return matches
        return []

    def selector_for(self, role: str) -> str | None:
        """First CSS selector that currently resolves to >=1 usable element.

        Used for the capture DOM read (querySelectorAll); returns None if the role
        only resolves via the text/vision strategies (no plain selector)."""
        for sel in self._css_candidates(role):
            if self._usable_matches(sel, limit=1):
                return sel
        return None

    def field_selector(self, role: str) -> str | None:
        """Best sub-element CSS for a FIELD role (author/headline/text/actions row),
        used inside a post element and in the capture read. Returns the first profile
        or generic candidate (not visibility-filtered — it's a within-post lookup)."""
        hints = self.profile.roles.get(role)
        cands = list(hints.css) if hints else []
        cands.extend(GENERIC_FIELD_CSS.get(role, ()))
        return cands[0] if cands else None

    def explain(self, role: str) -> dict[str, Any]:
        """Diagnostic: which strategy resolves a clickable role right now, and how."""
        hints = self.profile.roles.get(role)
        for sel in self._css_candidates(role):
            matches = self._usable_matches(sel, limit=8)
            if matches:
                strat = "profile-css" if hints and sel in hints.css else "semantic-css"
                return {"role": role, "ok": True, "strategy": strat, "match": sel, "count": len(matches)}
        for label in self._text_labels(role):
            if self._by_text(label) is not None:
                strat = "profile-text" if hints and label in hints.text else "default-text"
                return {"role": role, "ok": True, "strategy": strat, "match": f"text:{label}", "count": 1}
        return {"role": role, "ok": False, "strategy": "vision-or-none", "match": None, "count": 0}

    def explain_field(self, role: str) -> dict[str, Any]:
        """Diagnostic: which sub-element selector resolves a field role (page-wide)."""
        for sel in self.field_candidates(role):
            try:
                count = self.page.locator(sel).count()
            except Exception:
                count = 0
            if count > 0:
                return {"role": role, "ok": True, "strategy": "field-css", "match": sel, "count": count}
        return {"role": role, "ok": False, "strategy": "none", "match": None, "count": 0}

    def field_candidates(self, role: str) -> list[str]:
        hints = self.profile.roles.get(role)
        cands = list(hints.css) if hints else []
        cands.extend(GENERIC_FIELD_CSS.get(role, ()))
        seen: set[str] = set()
        return [c for c in cands if not (c in seen or seen.add(c))]


# =========================================================================== #
# Built-in profiles
# =========================================================================== #
GAUNTLET_PROFILE = AutomationProfile(
    name="gauntlet",
    kind="feed",
    roles={
        "feed_post": RoleHints(css=("article.g-feed-post", ".g-feed-post")),
        "feed_author": RoleHints(css=(".name",)),
        "feed_headline": RoleHints(css=(".meta",)),
        "feed_text": RoleHints(css=(".text",)),
        "feed_actions_row": RoleHints(css=(".actions",)),
        "feed_like": RoleHints(css=(".g-feed-like",)),
        "nav_home": RoleHints(css=("#g-nav-home",), text=("Home",)),
        "nav_notifications": RoleHints(css=("#g-nav-notifications",), text=("Alerts", "Notifications")),
        "nav_messages": RoleHints(css=("#g-nav-messages",), text=("Messaging",)),
        "nav_network": RoleHints(css=("#g-nav-network",), text=("Network",)),
        "nav_jobs": RoleHints(css=("#g-nav-jobs",), text=("Jobs",)),
        "search_input": RoleHints(css=("#g-search-input",)),
        "search_submit": RoleHints(css=("#g-search-go",)),
        "result_name": RoleHints(css=(".g-result-name",)),
        "profile_section": RoleHints(css=(".g-profile-section",)),
        "profile_back": RoleHints(css=("#g-profile-back",)),
    },
    campaign={"interest_terms": ["data engineer", "ml platform", "analytics lead", "staff data engineer", "head of data"]},
)

LINKEDIN_PROFILE = AutomationProfile(
    name="linkedin",
    kind="feed",
    roles={
        "feed_post": RoleHints(css=("div.feed-shared-update-v2[data-urn]", "div.fie-impression-container", "[role=feed] [role=article]", "[data-urn]")),
        "feed_author": RoleHints(css=(".update-components-actor__title", ".update-components-actor__name")),
        "feed_headline": RoleHints(css=(".update-components-actor__description",)),
        "feed_text": RoleHints(css=(".update-components-text", ".feed-shared-update-v2__description")),
        "feed_actions_row": RoleHints(css=(".feed-shared-social-action-bar", ".social-actions-buttons")),
        "feed_like": RoleHints(css=("button[aria-label*='React Like' i]", "button[aria-label*='Like' i]"), text=("Like",)),
        "nav_home": RoleHints(css=("a[href$='/feed/']", "a[data-test-global-nav-link='home']"), text=("Home",)),
        "nav_notifications": RoleHints(css=("a[href*='/notifications/']",), text=("Notifications",)),
        "nav_messages": RoleHints(css=("a[href*='/messaging/']",), text=("Messaging",)),
        "nav_network": RoleHints(css=("a[href*='/mynetwork/']",), text=("My Network",)),
        "nav_jobs": RoleHints(css=("a[href*='/jobs/']",), text=("Jobs",)),
        "search_input": RoleHints(css=("input.search-global-typeahead__input", "input[role=combobox][aria-label*='Search' i]", "input[placeholder*='Search' i]")),
        "search_submit": RoleHints(css=("button.search-global-typeahead__search-icon-button",), text=("Search",)),
        "result_name": RoleHints(css=(".entity-result__title-text a", ".reusable-search__result-container a[href*='/in/']", "a[href*='/in/']")),
        "profile_section": RoleHints(css=("section.artdeco-card",)),
        "profile_back": RoleHints(css=("button[aria-label*='Back' i]",), text=("Back",)),
    },
    campaign={},
)

_BUILTIN_BY_NAME = {"gauntlet": GAUNTLET_PROFILE, "linkedin": LINKEDIN_PROFILE}


def builtin_profile_for_url(url: str) -> AutomationProfile:
    u = (url or "").lower()
    if "linkedin.com" in u:
        return LINKEDIN_PROFILE
    if "/gauntlet" in u or ":5190" in u:
        return GAUNTLET_PROFILE
    return AutomationProfile()  # generic: semantic + text strategies only


def resolve_profile(
    plan: dict[str, Any] | None = None,
    url: str | None = None,
) -> AutomationProfile:
    """Pick the automation profile for a run.

    Priority: an explicit ``automation_profile`` dict on the plan (e.g. loaded from
    the website entry) > a named built-in > the URL-matched built-in > generic.
    """
    if isinstance(plan, dict):
        ap = plan.get("automation_profile")
        if isinstance(ap, dict) and ap.get("roles"):
            return AutomationProfile.from_dict(ap)
        named = ap.get("name") if isinstance(ap, dict) else (ap if isinstance(ap, str) else None)
        if isinstance(named, str) and named in _BUILTIN_BY_NAME:
            return _BUILTIN_BY_NAME[named]
        url = url or plan.get("url") or plan.get("start_url")
    return builtin_profile_for_url(url or "")
