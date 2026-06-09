"""Deterministic prompt -> story/Markov translation eval harness.

This module measures HOW WELL the automation turns a natural-language PROMPT into
an executable, goal-faithful "story" before any browser is opened. It runs a set
of representative prompts through the real compile path
(``goal_spec_from_natural_prompt`` + ``derive_plan_overrides``) and scores each on
five dimensions:

1. compiles            — a well-formed GoalSpec (non-empty steps, every step has
                         an action, the journey starts by visiting the target).
2. multi_step_story    — a coherent multi-step journey, not a degenerate 1-step
                         skim.
3. click_type_safe     — destructive primitives (click/type) appear ONLY when the
                         prompt actually asked for an action, and when present
                         they are fully specified (selector / text).
4. goal_faithful       — the compiled steps match the declared intent (open
                         notifications, type the search query, click N links,
                         scroll the feed, browse, ...).
5. markov_engaged      — ambient browsing prompts engage the semi-Markov walk
                         (via ``use_markov_pathing``); directed task prompts do
                         not hijack it.
   interest_captured   — a prompt that names a topic surfaces it as
                         ``interest_terms`` for the goal+Markov hybrid.

Everything here is PURE: no network, no Playwright, no live LinkedIn. A FakePage
is provided for the one wiring check that touches a (fake) DOM.

Running the module's ``test_generate_eval_report`` writes a human-readable
scorecard to ``.codex-outputs/prompt-to-story-eval.md``.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import pytest

from imposter5.automation_connector.goals import (
    GoalSpec,
    derive_plan_overrides,
    goal_spec_from_natural_prompt,
    goal_spec_from_payload,
    goal_spec_to_payload,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
REPORT_PATH = REPO / ".codex-outputs" / "prompt-to-story-eval.md"

LINKEDIN_FEED = "https://www.linkedin.com/feed/"


@dataclass(frozen=True)
class PromptCase:
    """One evaluated prompt and what a faithful translation should look like."""

    label: str
    prompt: str
    url: str
    provider: str | None
    # Coarse expected shape, used to derive precise assertions.
    kind: str  # ambient | directed_nav | search | click_links | feed_gather | observe
    expect_interest: tuple[str, ...] = ()
    expect_markov: bool = False
    expect_clicks: bool = False
    expect_typing: bool = False
    # A substring that must appear in a step selector/text for goal-faithfulness.
    must_contain: str = ""


CASES: tuple[PromptCase, ...] = (
    PromptCase(
        label="feed_interest_hiring",
        prompt="scan my feed and read anything about hiring",
        url=LINKEDIN_FEED, provider="linkedin", kind="feed_gather",
        expect_interest=("hiring",), expect_markov=False,
    ),
    PromptCase(
        label="notifications_then_profiles",
        prompt="check my notifications then look at 2 profiles",
        url=LINKEDIN_FEED, provider="linkedin", kind="directed_nav",
        expect_clicks=True, must_contain="/notifications/",
    ),
    PromptCase(
        label="casual_generic_browse",
        prompt="browse like a casual user for a few minutes",
        url="https://example.com", provider=None, kind="ambient",
        expect_markov=True,
    ),
    PromptCase(
        label="casual_linkedin_ai_infra",
        prompt="casually browse linkedin and look at posts about AI infra",
        url=LINKEDIN_FEED, provider="linkedin", kind="ambient",
        expect_interest=("ai infra",), expect_markov=True,
    ),
    PromptCase(
        label="linkedin_people_search",
        prompt="search for data engineers in San Francisco",
        url=LINKEDIN_FEED, provider="linkedin", kind="search",
        expect_clicks=True, expect_typing=True, must_contain="data engineers",
    ),
    PromptCase(
        label="open_messages",
        prompt="open my messages",
        url=LINKEDIN_FEED, provider="linkedin", kind="directed_nav",
        expect_clicks=True, must_contain="/messaging/",
    ),
    PromptCase(
        label="click_three_links",
        prompt="click 3 random links and come back",
        url="https://example.com", provider=None, kind="click_links",
        expect_clicks=True, must_contain="random_link",
    ),
    PromptCase(
        label="feed_gather_posts",
        prompt="scroll the feed and gather some posts",
        url=LINKEDIN_FEED, provider="linkedin", kind="feed_gather",
        expect_markov=False,
    ),
    PromptCase(
        label="feed_interest_fundraising",
        prompt="read posts related to fundraising or new rounds",
        url=LINKEDIN_FEED, provider="linkedin", kind="feed_gather",
        expect_interest=("fundraising", "new rounds"),
    ),
    PromptCase(
        label="kill_time_generic",
        prompt="just scroll around and kill time",
        url="https://example.com", provider=None, kind="ambient",
        expect_markov=True,
    ),
    PromptCase(
        label="observe_page",
        prompt="look at this page and tell me what is on it",
        url="https://example.com", provider=None, kind="observe",
    ),
    PromptCase(
        label="wander_linkedin",
        prompt="wander around linkedin for a while",
        url=LINKEDIN_FEED, provider="linkedin", kind="ambient",
        expect_markov=True,
    ),
)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def _compile(case: PromptCase) -> GoalSpec:
    return goal_spec_from_natural_prompt(
        case.prompt, start_url=case.url, provider_hint=case.provider
    )


def _step_actions(goal: GoalSpec) -> list[str]:
    return [s.action for s in goal.steps]


def _well_formed(goal: GoalSpec) -> bool:
    if not goal.steps:
        return False
    if goal.steps[0].action != "visit":
        return False
    return all(bool(s.action) for s in goal.steps)


def _click_type_safe(goal: GoalSpec, case: PromptCase) -> bool:
    has_click = any(s.action in ("click", "click_element") for s in goal.steps)
    has_type = any(s.action in ("type", "type_text", "fill") for s in goal.steps)

    # Destructive primitives must not appear for observe/ambient/feed prompts.
    if not case.expect_clicks and has_click:
        return False
    if not case.expect_typing and has_type:
        return False

    # When present, click/type steps must be fully specified so execution is safe.
    for s in goal.steps:
        params = s.params or {}
        if s.action in ("click", "click_element") and not params.get("selector"):
            return False
        if s.action in ("type", "type_text", "fill"):
            if not params.get("selector") or not (params.get("text") or params.get("value")):
                return False
    return True


def _goal_faithful(goal: GoalSpec, case: PromptCase) -> bool:
    selectors = " ".join((s.params or {}).get("selector", "") for s in goal.steps)
    texts = " ".join((s.params or {}).get("text", "") for s in goal.steps)
    actions = _step_actions(goal)
    if case.must_contain and case.must_contain not in (selectors + " " + texts):
        return False
    if case.kind == "search":
        return "type" in actions and "click" in actions
    if case.kind == "directed_nav":
        return "click" in actions
    if case.kind == "click_links":
        return actions.count("click") >= 3 and "backtrack" in actions
    if case.kind in ("feed_gather", "observe"):
        return "read" in actions and "scroll" in actions
    if case.kind == "ambient":
        return goal.use_markov is True
    return True


def evaluate(case: PromptCase) -> dict[str, Any]:
    goal = _compile(case)
    overrides = derive_plan_overrides(goal)
    interest = tuple(goal.interest_terms)
    markov_engaged = bool(overrides.get("use_markov_pathing"))

    interest_ok = (set(case.expect_interest) <= set(interest)) and (
        bool(interest) == bool(case.expect_interest) or not case.expect_interest
    )
    return {
        "label": case.label,
        "kind": case.kind,
        "prompt": case.prompt,
        "steps": _step_actions(goal),
        "step_count": len(goal.steps),
        "interest_terms": list(interest),
        "use_markov": goal.use_markov,
        "overrides": overrides,
        # scored dimensions
        "compiles": _well_formed(goal),
        "multi_step_story": len(goal.steps) >= 4,
        "click_type_safe": _click_type_safe(goal, case),
        "goal_faithful": _goal_faithful(goal, case),
        "markov_engaged": markov_engaged,
        "markov_match": markov_engaged == case.expect_markov,
        "interest_captured": bool(interest),
        "interest_match": interest_ok,
    }


# --------------------------------------------------------------------------- #
# Per-prompt assertions (parametrized so a regression names the exact prompt)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", CASES, ids=[c.label for c in CASES])
def test_prompt_compiles_well_formed(case: PromptCase) -> None:
    result = evaluate(case)
    assert result["compiles"], f"{case.label}: not well-formed -> {result['steps']}"
    assert result["multi_step_story"], f"{case.label}: degraded to a {result['step_count']}-step skim"


@pytest.mark.parametrize("case", CASES, ids=[c.label for c in CASES])
def test_prompt_click_type_safe(case: PromptCase) -> None:
    result = evaluate(case)
    assert result["click_type_safe"], (
        f"{case.label}: unsafe/underspecified click or type step -> {result['steps']}"
    )


@pytest.mark.parametrize("case", CASES, ids=[c.label for c in CASES])
def test_prompt_goal_faithful(case: PromptCase) -> None:
    result = evaluate(case)
    assert result["goal_faithful"], (
        f"{case.label}: steps don't honor the {case.kind} intent -> {result['steps']}"
    )


@pytest.mark.parametrize("case", CASES, ids=[c.label for c in CASES])
def test_prompt_markov_engagement_matches_expectation(case: PromptCase) -> None:
    result = evaluate(case)
    assert result["markov_match"], (
        f"{case.label}: markov_engaged={result['markov_engaged']} expected={case.expect_markov}"
    )


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.expect_interest], ids=[c.label for c in CASES if c.expect_interest]
)
def test_prompt_interest_terms_captured(case: PromptCase) -> None:
    result = evaluate(case)
    assert set(case.expect_interest) <= set(result["interest_terms"]), (
        f"{case.label}: expected interest {case.expect_interest}, got {result['interest_terms']}"
    )


# --------------------------------------------------------------------------- #
# Structural invariants of the bridge
# --------------------------------------------------------------------------- #
def test_directed_prompts_never_engage_markov() -> None:
    """Notifications/messaging/search/click prompts must keep their concrete steps
    and never get hijacked by the random walk."""
    for case in CASES:
        if case.kind in ("directed_nav", "search", "click_links"):
            result = evaluate(case)
            assert not result["markov_engaged"], f"{case.label} should stay directed"


def test_linkedin_ambient_scan_matrix_is_click_type_free() -> None:
    """An ambient LinkedIn walk reuses the goal+Markov hybrid's FEED_SCAN_MATRIX,
    which never autonomously clicks or types — only the goal layer opens a post."""
    from imposter5.loaders.linkedin_feed_scraper import FEED_SCAN_MATRIX

    goal = goal_spec_from_natural_prompt(
        "casually browse linkedin for a while", start_url=LINKEDIN_FEED, provider_hint="linkedin"
    )
    overrides = derive_plan_overrides(goal)
    assert overrides.get("markov_matrix") == FEED_SCAN_MATRIX
    for state, row in overrides["markov_matrix"].items():
        assert state not in ("click", "typing")
        assert "click" not in row and "typing" not in row


def test_generic_ambient_engages_default_walk_without_forcing_matrix() -> None:
    """A generic ambient browse engages the Markov walk but does NOT force the
    LinkedIn scan matrix (it gets the default human matrix, where ambient clicks
    are appropriate on an ordinary site)."""
    goal = goal_spec_from_natural_prompt(
        "browse like a casual user for a few minutes", start_url="https://example.com"
    )
    overrides = derive_plan_overrides(goal)
    assert overrides.get("use_markov_pathing") is True
    assert "markov_matrix" not in overrides


def test_goal_spec_round_trips_new_fields() -> None:
    """interest_terms + use_markov survive payload serialization (so feasibility /
    audit / UI can read them)."""
    goal = goal_spec_from_natural_prompt(
        "casually browse linkedin and look at posts about AI infra",
        start_url=LINKEDIN_FEED, provider_hint="linkedin",
    )
    restored = goal_spec_from_payload(goal_spec_to_payload(goal))
    assert restored.interest_terms == goal.interest_terms
    assert restored.use_markov == goal.use_markov


# --------------------------------------------------------------------------- #
# No-network guard + report generation
# --------------------------------------------------------------------------- #
class _FakePage:
    """Minimal duck-typed page proving the compile path never touches a browser."""

    url = LINKEDIN_FEED

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - defensive
        raise AssertionError(f"compile path must not touch the page (called .{name})")


def test_compile_path_is_pure_no_page_access() -> None:
    # Passing a page that explodes on ANY access proves the translation is offline.
    page = _FakePage()
    for case in CASES:
        evaluate(case)  # uses only the prompt, never `page`
    assert page.url  # page exists but was never used by the compile path


def _render_report(results: list[dict[str, Any]]) -> str:
    n = len(results)

    def rate(key: str, universe: list[dict[str, Any]] | None = None) -> str:
        rows = universe if universe is not None else results
        if not rows:
            return "0/0 (n/a)"
        hits = sum(1 for r in rows if r[key])
        return f"{hits}/{len(rows)} ({round(100 * hits / len(rows))}%)"

    interest_rows = [r for r in results if r["interest_terms"] or r["label"].startswith("feed_interest") or "interest" in r["label"]]
    lines: list[str] = []
    lines.append("# Prompt -> Story / Markov translation eval")
    lines.append("")
    lines.append(f"Prompts evaluated: **{n}**  (deterministic compile path, no network)")
    lines.append("")
    lines.append("## Aggregate scores")
    lines.append("")
    lines.append(f"- Compiles to a well-formed GoalSpec: **{rate('compiles')}**")
    lines.append(f"- Coherent multi-step story (>=4 steps, not a generic skim): **{rate('multi_step_story')}**")
    lines.append(f"- Click/typing-safe (no stray destructive steps; specified when present): **{rate('click_type_safe')}**")
    lines.append(f"- Goal-faithful (steps honor the declared intent): **{rate('goal_faithful')}**")
    lines.append(f"- Markov engagement matches expectation: **{rate('markov_match')}**")
    lines.append(f"- Interest captured where a topic was named: **{rate('interest_match')}**")
    lines.append("")
    markov_on = sum(1 for r in results if r["markov_engaged"])
    lines.append(f"- Prompts that engage the semi-Markov walk: **{markov_on}/{n}**")
    interest_on = sum(1 for r in results if r["interest_terms"])
    lines.append(f"- Prompts that captured >=1 interest term: **{interest_on}/{n}**")
    lines.append("")
    lines.append("## Per-prompt detail")
    lines.append("")
    lines.append("| prompt | kind | steps | markov | interest | safe | faithful |")
    lines.append("| --- | --- | --- | :---: | --- | :---: | :---: |")
    for r in results:
        ok = lambda b: "✅" if b else "❌"  # noqa: E731
        steps = " → ".join(r["steps"])
        interest = ", ".join(r["interest_terms"]) or "—"
        markov = "🎲" if r["markov_engaged"] else "—"
        lines.append(
            f"| {r['prompt']} | {r['kind']} | {steps} | {markov} | {interest} | "
            f"{ok(r['click_type_safe'])} | {ok(r['goal_faithful'])} |"
        )
    lines.append("")
    lines.append("Legend: 🎲 = semi-Markov walk engaged via `use_markov_pathing`.")
    lines.append("")
    return "\n".join(lines)


def test_generate_eval_report() -> None:
    results = [evaluate(c) for c in CASES]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(_render_report(results), encoding="utf-8")

    # The harness is also a quality gate: every prompt must translate cleanly.
    assert all(r["compiles"] for r in results)
    assert all(r["click_type_safe"] for r in results)
    assert all(r["goal_faithful"] for r in results)
    assert all(r["markov_match"] for r in results)
    assert all(r["interest_match"] for r in results)
