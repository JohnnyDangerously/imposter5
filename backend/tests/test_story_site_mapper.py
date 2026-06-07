from __future__ import annotations

from imposter5.story.site_mapper import SiteMapper


def _reveal_results(page) -> None:
    page.locator("#g-search-input").click()
    page.locator("#g-search-input").fill("data engineers")
    page.locator("#g-search-go").click()
    page.wait_for_timeout(150)


def test_mapper_resolves_search_affordances_generically(gauntlet_page) -> None:
    mapper = SiteMapper(gauntlet_page)
    amap = mapper.map_view(refresh=True)
    # Resolved by role/landmark/attribute heuristics, not by literal #g- ids.
    assert amap.selector("search_input") is not None
    assert amap.selector("search_submit") is not None
    assert amap.selector("nav_target") is not None
    # The winning selectors must be GENERIC, never gauntlet-specific ids.
    for role, sel in amap.selectors.items():
        if sel:
            assert "#g-" not in sel, f"{role} resolved to a hard-coded id: {sel}"


def test_mapper_excludes_honeypots_from_results(gauntlet_page) -> None:
    _reveal_results(gauntlet_page)
    mapper = SiteMapper(gauntlet_page)
    items = mapper.resolve_all("result_item")
    # 26 cards exist in the DOM (24 real + 2 honeypot traps); only the 24 real,
    # human-reachable cards must be resolved.
    assert len(items) == 24, f"expected 24 usable result cards, got {len(items)}"
    # No resolved item is a trap.
    keys = set()
    for it in items:
        pid = it.get_attribute("data-person-id") or ""
        keys.add(pid)
    assert not any("trap" in k for k in keys), f"honeypot leaked into results: {keys}"


def test_mapper_resolves_result_open_links(gauntlet_page) -> None:
    _reveal_results(gauntlet_page)
    mapper = SiteMapper(gauntlet_page)
    opens = mapper.resolve_all("result_open")
    assert len(opens) >= 20
    for a in opens:
        href = a.get_attribute("href") or ""
        assert "#trap" not in href, "honeypot link must not be resolvable"


def test_mapper_resolves_profile_view_affordances(gauntlet_page) -> None:
    _reveal_results(gauntlet_page)
    # Open a profile by clicking the first real result name.
    gauntlet_page.locator(".g-result-card[data-person-id='person-1000'] .g-result-name").click()
    gauntlet_page.wait_for_timeout(150)
    mapper = SiteMapper(gauntlet_page)
    assert mapper.resolve_one("back_control") is not None
    sections = mapper.resolve_all("profile_section")
    assert len(sections) >= 3
