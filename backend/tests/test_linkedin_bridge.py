"""Tests for the organic-prompt -> LinkedIn parity bridges.

Covers the prompt compiler's LinkedIn intent literacy (bridge 2) and the goal
runner's LinkedIn-aware structured extraction (bridge 3). The authenticated
session bridge (bridge 1) is exercised via the live run path, not unit-tested
here (it needs a real persistent browser context).
"""
from __future__ import annotations

from typing import Any

from imposter5.automation_connector import goal_runner
from imposter5.automation_connector.goal_runner import run_visible_state_goal
from imposter5.automation_connector.goals import goal_spec_from_natural_prompt
from imposter5.automation_connector.session_recorder import SessionRecorder


# --- Bridge 2: compiler site literacy --------------------------------------- #
def _actions(prompt: str) -> list[tuple[str, str]]:
    goal = goal_spec_from_natural_prompt(
        prompt, start_url="https://www.linkedin.com/feed/", provider_hint="linkedin"
    )
    return [(s.action, s.name) for s in goal.steps]


def test_linkedin_feed_prompt_compiles_to_scroll_and_read() -> None:
    actions = _actions("scroll the feed and gather some posts")
    names = [a for a, _ in actions]
    assert names == ["visit", "wait", "scroll", "read", "record"]


def test_linkedin_notifications_prompt_targets_notifications_nav() -> None:
    goal = goal_spec_from_natural_prompt(
        "check my notifications", start_url="https://www.linkedin.com/feed/", provider_hint="linkedin"
    )
    click = next(s for s in goal.steps if s.action == "click")
    assert "/notifications/" in (click.params or {}).get("selector", "")


def test_linkedin_messaging_prompt_targets_messaging_nav() -> None:
    goal = goal_spec_from_natural_prompt(
        "open my messages", start_url="https://www.linkedin.com/feed/", provider_hint="linkedin"
    )
    click = next(s for s in goal.steps if s.action == "click")
    assert "/messaging/" in (click.params or {}).get("selector", "")


def test_linkedin_search_prompt_extracts_query_and_emits_search_steps() -> None:
    goal = goal_spec_from_natural_prompt(
        "search for data engineers", start_url="https://www.linkedin.com/feed/", provider_hint="linkedin"
    )
    type_step = next(s for s in goal.steps if s.action == "type")
    assert type_step.params["text"] == "data engineers"
    # The submit click is best-effort (LinkedIn search also submits on Enter).
    submit = next(s for s in goal.steps if s.name == "submit_search")
    assert submit.required is False


def test_linkedin_detection_via_url_without_provider_hint() -> None:
    goal = goal_spec_from_natural_prompt("scroll the feed", start_url="https://linkedin.com/feed/")
    assert [s.action for s in goal.steps] == ["visit", "wait", "scroll", "read", "record"]


# --- Bridge 3: runner LinkedIn-aware extraction ----------------------------- #
class _FakeMouse:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events

    def wheel(self, dx: int, dy: int) -> None:
        self.events.append(("wheel", {"delta_x": dx, "delta_y": dy}))

    def move(self, x: float, y: float) -> None:
        self.events.append(("mouse_move", {"x": round(x), "y": round(y)}))


class _FakeLinkedInPage:
    viewport_size = {"width": 1280, "height": 800}
    url = "https://www.linkedin.com/feed/"

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.mouse = _FakeMouse(self.events)

    def goto(self, url: str, *, wait_until: str) -> None:
        self.events.append(("goto", url))

    def wait_for_timeout(self, ms: int) -> None:
        self.events.append(("wait", ms))

    def title(self) -> str:
        return "Feed | LinkedIn"

    def inner_text(self, selector: str) -> str:
        return "Feed post ..."

    # Reading-variation helpers probe these; empty results keep them no-ops.
    def query_selector_all(self, selector: str) -> list[Any]:
        return []


def test_runner_extracts_structured_posts_on_linkedin(monkeypatch) -> None:
    fake_posts = [
        {"actor_name": "Casey", "post_text": "hi", "post_url": "https://www.linkedin.com/feed/update/1"},
        {"actor_name": "Dana", "post_text": "yo", "post_url": "https://www.linkedin.com/feed/update/2"},
    ]
    import imposter5.loaders.linkedin_feed_scraper as li

    monkeypatch.setattr(li, "extract_visible_posts", lambda page: fake_posts)

    page = _FakeLinkedInPage()
    goal = goal_spec_from_natural_prompt(
        "scroll the feed", start_url="https://www.linkedin.com/feed/", provider_hint="linkedin"
    )
    plan = {"run_id": "run-li", "completion": {"max_scroll_passes": 1}}
    result = run_visible_state_goal(page, goal, plan, recorder=SessionRecorder(plan))

    # Structured posts surfaced (deduped), not just a text blob.
    assert isinstance(result["linkedin_posts"], list)
    assert len(result["linkedin_posts"]) == 2
    assert {p["actor_name"] for p in result["linkedin_posts"]} == {"Casey", "Dana"}


def test_runner_no_linkedin_posts_off_platform() -> None:
    from imposter5.automation_connector.goals import goal_spec_from_target

    class _Page(_FakeLinkedInPage):
        url = "https://example.com/accounts"

        def inner_text(self, selector: str) -> str:
            return "Generic content."

    goal = goal_spec_from_target({"entity_id": "https://example.com/accounts"})
    plan = {"run_id": "run-x", "completion": {"max_scroll_passes": 1}}
    result = run_visible_state_goal(_Page(), goal, plan, recorder=SessionRecorder(plan))
    assert result["linkedin_posts"] == []
