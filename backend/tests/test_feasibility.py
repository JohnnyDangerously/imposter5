"""Tests for pre-execution feasibility / action review (workstream C).

Uses lightweight fake page/locator/element doubles (in the spirit of
``test_automation_connector_interaction_primitives.py``) so the SiteMapper-backed
dry-run can be exercised without a real browser. Each fake page is backed by a tiny
selector -> elements registry; ``SiteMapper`` and the feasibility probe both read it,
so a target "exists" exactly when the registry has a visible element for the selector
(or one of its comma-separated parts).
"""
from __future__ import annotations

from typing import Any

from imposter5.automation_connector.feasibility import FeasibilityReport, review_feasibility
from imposter5.automation_connector.goals import (
    GoalSpec,
    GoalStep,
    goal_spec_from_natural_prompt,
)


class FakeElement:
    def __init__(self, text: str = "", *, visible: bool = True, attrs: dict[str, str] | None = None) -> None:
        self._text = text
        self._visible = visible
        self._attrs = attrs or {}

    @property
    def first(self) -> "FakeElement":
        return self

    def evaluate(self, script: str) -> str:
        # Honeypot / negative-offscreen probes run el-scoped JS; the fake is never a trap.
        return ""

    def is_visible(self) -> bool:
        return self._visible

    def inner_text(self) -> str:
        return self._text

    def text_content(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)


class FakeLocator:
    def __init__(self, elements: list[FakeElement]) -> None:
        self._elements = list(elements)

    @property
    def first(self) -> FakeElement:
        return self._elements[0] if self._elements else FakeElement(visible=False)

    def all(self) -> list[FakeElement]:
        return list(self._elements)

    def count(self) -> int:
        return len(self._elements)

    def is_visible(self) -> bool:
        return bool(self._elements) and self._elements[0].is_visible()

    def evaluate(self, script: str) -> str:
        return ""


class FakePage:
    """Selector-registry backed page double.

    ``dom`` maps a literal CSS selector to the list of elements it matches. A query
    for a comma-separated selector returns the union across its parts, mirroring how
    Playwright treats selector lists (so SiteMapper's single-candidate probes and the
    compiler's comma selectors both resolve from one registry).
    """

    def __init__(self, dom: dict[str, list[FakeElement]] | None = None, url: str = "https://target.test/") -> None:
        self.dom = dom or {}
        self.url = url

    def locator(self, selector: str) -> FakeLocator:
        elements: list[FakeElement] = []
        seen: set[int] = set()
        for part in str(selector).split(","):
            key = part.strip()
            if not key:
                continue
            for el in self.dom.get(key, []):
                if id(el) not in seen:
                    seen.add(id(el))
                    elements.append(el)
        return FakeLocator(elements)


def test_sandbox_goal_with_all_affordances_is_ok() -> None:
    goal = goal_spec_from_natural_prompt("audit the sandbox", "https://target.test/")
    page = FakePage(
        dom={
            "#sandbox": [FakeElement("Sandbox")],
            "#text-input": [FakeElement()],
            "#submit-btn": [FakeElement("Submit")],
        }
    )

    report = review_feasibility(page, goal)

    assert isinstance(report, FeasibilityReport)
    assert report.status == "ok"
    assert report.blocks_run is False
    # Every targeted (click/type) step must have resolved.
    targeted = [s for s in report.steps if s.action in {"click", "type"}]
    assert targeted, "sandbox flow should contain click/type steps"
    assert all(s.feasible for s in targeted)


def test_sandbox_goal_missing_affordance_is_infeasible() -> None:
    goal = goal_spec_from_natural_prompt("audit the sandbox", "https://target.test/")
    page = FakePage(dom={})  # the sandbox controls do not exist on this page

    report = review_feasibility(page, goal)

    assert report.status == "infeasible"
    assert report.blocks_run is True
    blocked = [s for s in report.steps if s.required and not s.feasible]
    assert blocked, "missing #sandbox control should block the run"
    # The reason is human-readable and names the missing target.
    assert any("#sandbox" in s.reason for s in blocked)
    assert "cannot be performed" in report.summary


def test_named_affordance_present_is_ok() -> None:
    goal = GoalSpec(
        name="open_messages",
        start_url="https://target.test/",
        desired_outcome="prompt_executed",
        steps=(
            GoalStep("visit_start_url", "visit"),
            GoalStep("click_nav_messages", "click", params={"label": "Messages"}),
        ),
    )
    page = FakePage(dom={"nav a": [FakeElement("Home"), FakeElement("Messages"), FakeElement("Profile")]})

    report = review_feasibility(page, goal)

    assert report.status == "ok"
    assert report.blocks_run is False


def test_named_affordance_absent_is_infeasible_with_reason() -> None:
    goal = GoalSpec(
        name="open_messages",
        start_url="https://target.test/",
        desired_outcome="prompt_executed",
        steps=(
            GoalStep("visit_start_url", "visit"),
            GoalStep("click_nav_messages", "click", params={"label": "Messages"}),
        ),
    )
    # Nav exists, but there is no "Messages" entry on this page.
    page = FakePage(dom={"nav a": [FakeElement("Home"), FakeElement("Profile")]})

    report = review_feasibility(page, goal)

    assert report.status == "infeasible"
    assert report.blocks_run is True
    reasons = [s.reason for s in report.steps if not s.feasible]
    assert any("Messages" in r for r in reasons)
    assert any("no 'Messages' affordance found on this page" == r for r in reasons)


def test_observe_only_goal_does_not_block() -> None:
    # The generic observation flow (no click/type) must never block a run.
    goal = goal_spec_from_natural_prompt("just look at the page", "https://target.test/")
    page = FakePage(dom={})

    report = review_feasibility(page, goal)

    assert report.blocks_run is False
    assert report.status == "ok"
    assert all(s.feasible for s in report.steps)


def test_search_goal_resolves_via_semantic_affordance() -> None:
    # The compiler emits illustrative selector LISTS; resolution should still succeed
    # when the page only exposes a generic search affordance SiteMapper recognizes.
    goal = goal_spec_from_natural_prompt("search for data engineers", "https://target.test/")
    page = FakePage(
        dom={
            "input[type=search]": [FakeElement()],  # SiteMapper: search_input
            "button[type=submit]": [FakeElement("Go")],  # submit_search literal selector
        }
    )

    report = review_feasibility(page, goal)

    assert report.status == "ok"
    assert report.blocks_run is False


def test_search_goal_on_page_without_search_is_infeasible() -> None:
    goal = goal_spec_from_natural_prompt("search for data engineers", "https://target.test/")
    page = FakePage(dom={})

    report = review_feasibility(page, goal)

    assert report.status == "infeasible"
    assert report.blocks_run is True


def test_unmappable_page_is_skipped_not_blocking() -> None:
    class ExplodingPage:
        url = "https://target.test/"

        def locator(self, selector: str) -> Any:
            raise RuntimeError("DOM is gone")

    goal = goal_spec_from_natural_prompt("audit the sandbox", "https://target.test/")

    report = review_feasibility(ExplodingPage(), goal)

    # A page we cannot map must not block (review is best-effort, fail-open).
    assert report.blocks_run is False
    assert report.status in {"ok", "skipped"}


def test_random_link_sentinel_resolves_when_links_exist() -> None:
    goal = goal_spec_from_natural_prompt("click 2 links", "https://target.test/")
    page = FakePage(dom={"a[href]": [FakeElement("A post"), FakeElement("Another")]})

    report = review_feasibility(page, goal)

    assert report.status == "ok"
    assert report.blocks_run is False


def test_payload_contract_is_stable() -> None:
    goal = goal_spec_from_natural_prompt("audit the sandbox", "https://target.test/")
    page = FakePage(dom={})

    payload = review_feasibility(page, goal).to_payload()

    assert set(payload) == {"status", "summary", "steps", "blocks_run"}
    assert payload["blocks_run"] is True
    assert isinstance(payload["steps"], list)
    for step in payload["steps"]:
        assert set(step) == {"step", "action", "feasible", "required", "reason"}
