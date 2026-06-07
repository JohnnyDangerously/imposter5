"""SiteMapper: resolve a live DOM to a GENERIC affordance map by role / landmark /
heuristics, so Story Mode works on ANY conformant site rather than a hard-coded
gauntlet layout.

Design rules (non-negotiable):
- NEVER hard-code site-specific element ids (e.g. ``#g-search-input``). Resolution
  is by accessibility role, ARIA/semantic landmark, attribute heuristics, and
  structural shape (a list with the most repeated link-bearing children, etc.).
- Honeypots (off-screen / tabindex=-1 / display:none / zero-opacity / 1px) are
  avoided by GENERAL principle. We reuse the existing, well-tested honeypot engine
  (``interaction_primitives._detect_honeypot_reason`` + ``_pick_visible_locator``)
  rather than re-deriving trap heuristics here.

The mapper caches which generic *selector* won for each role (heuristic v1, cached
per resolved view), but always RE-RESOLVES concrete elements live at execution
time, because the field is a single-page app whose visible view changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from imposter5.automation_connector.interaction_primitives import (
    _detect_honeypot_reason,
)

logger = logging.getLogger(__name__)

# The neutral affordance ROLES Story Mode reasons about. Each maps to an ordered
# list of GENERIC candidate selectors tried in priority order (most semantic first).
AFFORDANCE_ROLES = (
    "search_input",
    "search_submit",
    "result_list",
    "result_item",
    "result_open",
    "profile_view",
    "profile_section",
    "back_control",
    "nav_target",
)

# Ordered generic candidate selectors per role. These are role/landmark/attribute/
# structural heuristics that hold for "real websites", NOT gauntlet ids.
_ROLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "search_input": (
        "input[type=search]",
        "[role=searchbox]",
        "input[aria-label*='search' i]",
        "input[placeholder*='search' i]",
        "[role=search] input",
        "form[role=search] input",
        "input[name*='search' i]",
        "input[name='q']",
    ),
    "search_submit": (
        "[role=search] button",
        "form[role=search] button[type=submit]",
        "button[aria-label*='search' i]",
        "button[type=submit][aria-label*='search' i]",
        "[aria-label*='search' i][role=button]",
    ),
    "result_list": (
        "[role=feed]",
        "[role=list]",
        "main [role=list]",
        "ul[aria-label*='result' i]",
        "[aria-label*='results' i]",
        "main ul",
    ),
    "result_item": (
        "[role=listitem]",
        "[role=article]",
        "main li",
        "article",
        "[data-person-id]",
    ),
    # The clickable name/link that opens a result's detail view.
    "result_open": (
        "[role=listitem] a[href]",
        "[role=article] a[href]",
        "main li a[href]",
        "article a[href]",
        "[data-person-id] a[href]",
        "[role=listitem] a",
        "article a",
    ),
    "profile_view": (
        "[role=region][aria-label*='profile' i]",
        "main [aria-label*='profile' i]",
        "[role=main] article",
        "section[aria-label*='profile' i]",
    ),
    # Profile sections are inherently plural; prefer the repeated section signals
    # (the generic ``data-section`` attribute / ``section`` element) over a lone
    # ``[role=region]`` which often marks the whole profile view container.
    "profile_section": (
        "[data-section]",
        "[role=region] section",
        "main section section",
        "section[aria-label]",
        "article section",
    ),
    "back_control": (
        "button[aria-label*='back' i]",
        "[role=button][aria-label*='back' i]",
        "a[aria-label*='back' i]",
        "[aria-label='Back' i]",
    ),
    "nav_target": (
        "nav a",
        "[role=navigation] a",
        "header nav a",
        "[role=navigation] [role=link]",
    ),
}


@dataclass
class AffordanceMap:
    """Which generic selector won for each role on the currently mapped view.

    ``selectors[role]`` is the winning generic CSS selector (or None if no live,
    non-honeypot match exists right now). ``counts[role]`` is how many visible,
    non-honeypot elements that selector resolved to.
    """

    selectors: dict[str, str | None] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def selector(self, role: str) -> str | None:
        return self.selectors.get(role)

    def to_payload(self) -> dict[str, Any]:
        return {
            "selectors": dict(self.selectors),
            "counts": dict(self.counts),
        }


class SiteMapper:
    """Heuristic DOM -> affordance map (v1), with a per-view cache.

    ``page`` is a Playwright Page (or a compatible duck-typed object in tests).
    """

    def __init__(self, page: Any) -> None:
        self.page = page
        self._cache: dict[str, AffordanceMap] = {}

    # --- visibility / honeypot filtering (general principle, reused engine) -------
    # Distinguish a TRAP (parked in negative/far off-screen space, never reachable by
    # scrolling) from ordinary below-the-fold content (reachable by scrolling). This
    # is a general principle, not a gauntlet-specific rule.
    _NEGATIVE_OFFSCREEN_JS = r"""
    (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        // Parked off to negative space or absurdly far away => unreachable trap.
        return (r.right < 0) || (r.bottom < 0) || (r.left < -2000) || (r.top < -2000);
    }
    """

    def _is_negative_offscreen(self, locator: Any) -> bool:
        try:
            target = locator.first if hasattr(locator, "first") else locator
            return bool(target.evaluate(self._NEGATIVE_OFFSCREEN_JS))
        except Exception:
            return False

    def _is_usable(self, locator: Any) -> bool:
        """True if a concrete element is a real, human-reachable affordance.

        Reuses the existing honeypot engine (``_detect_honeypot_reason``) for every
        trap signature (display:none / visibility:hidden / opacity:0 / 1px / clip /
        tabindex=-1 / aria-hidden / text-indent). The engine's "offscreen" reason,
        however, also fires for content merely BELOW the current fold, which a human
        reaches by scrolling — so an offscreen-ONLY element is treated as a trap only
        when it is parked in negative/far space (truly unreachable).
        """
        try:
            if not locator.is_visible():
                return False
        except Exception:
            return False
        try:
            reason = _detect_honeypot_reason(locator)
        except Exception:
            # A flaky trap check must not block a genuinely visible element.
            logger.debug("[site_mapper] honeypot check raised; treating as usable")
            return True
        if not reason:
            return True
        reasons = {r for r in reason.split(",") if r}
        reasons.discard("offscreen")
        if reasons:
            return False  # a real trap signature other than below-the-fold position
        # Offscreen-only: reachable if it's just below/right of the fold.
        return not self._is_negative_offscreen(locator)

    def _usable_matches(self, selector: str, *, limit: int = 60) -> list[Any]:
        try:
            candidates = self.page.locator(selector).all()
        except Exception:
            return []
        usable: list[Any] = []
        for loc in candidates[:limit]:
            if self._is_usable(loc):
                usable.append(loc)
        return usable

    def _resolve_role(self, role: str) -> tuple[str | None, int]:
        """Return (winning_selector, usable_count) for a role, or (None, 0)."""
        for selector in _ROLE_CANDIDATES.get(role, ()):  # priority order
            usable = self._usable_matches(selector)
            if usable:
                return selector, len(usable)
        return None, 0

    # --- public API ---------------------------------------------------------------
    def map_view(self, *, view_key: str | None = None, refresh: bool = False) -> AffordanceMap:
        """Resolve all roles for the current view and cache by ``view_key``.

        ``view_key`` lets the SPA cache one map per logical view (feed/results/
        profile). When None, the current page URL is used.
        """
        key = view_key or self._current_key()
        if not refresh and key in self._cache:
            return self._cache[key]

        amap = AffordanceMap()
        for role in AFFORDANCE_ROLES:
            selector, count = self._resolve_role(role)
            amap.selectors[role] = selector
            amap.counts[role] = count
        self._cache[key] = amap
        return amap

    def _current_key(self) -> str:
        try:
            return str(self.page.url)
        except Exception:
            return "default"

    def resolve_one(self, role: str) -> Any | None:
        """Return the FIRST usable live element for a role, re-resolved now."""
        selector, _ = self._resolve_role(role)
        if not selector:
            return None
        matches = self._usable_matches(selector)
        return matches[0] if matches else None

    def resolve_all(self, role: str, *, limit: int = 60) -> list[Any]:
        """Return ALL usable live elements for a role, re-resolved now."""
        selector, _ = self._resolve_role(role)
        if not selector:
            return []
        return self._usable_matches(selector, limit=limit)

    def resolve_choice(self, role: str, rng: Any) -> Any | None:
        """Return a seeded random usable element for a role (off-goal target picks)."""
        matches = self.resolve_all(role)
        if not matches:
            return None
        return rng.choice(matches)
