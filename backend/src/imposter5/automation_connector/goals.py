"""Goal specifications for automation connector runs.

The goal layer describes intent and acceptable partial completion. Browser
runners and interaction primitives consume this shape, but the goal itself is
provider-neutral.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


DEFAULT_GOAL_NAME = "observe_visible_page_state"
DEFAULT_OUTCOME = "visible_state_recorded"


@dataclass(frozen=True)
class GoalStep:
    name: str
    action: str
    required: bool = True
    # For action steps from prompts: e.g. selector, text, value, etc.
    params: dict[str, Any] | None = None


@dataclass(frozen=True)
class GoalSpec:
    name: str
    start_url: str
    desired_outcome: str
    steps: tuple[GoalStep, ...]
    prompt: str = ""  # original user/agent natural language intent (for context + interp)
    # Topical interest/ICP terms parsed from the prompt (e.g. "read anything about
    # hiring" -> ("hiring",)). These feed the goal+Markov LinkedIn hybrid's
    # interest scoring (``linkedin_feed_scraper._resolve_interest_terms``) so the
    # session stops on what the user actually cares about instead of a generic
    # default vocabulary.
    interest_terms: tuple[str, ...] = ()
    # True when the prompt implies AMBIENT browsing ("browse like a casual user for
    # a few minutes", "scroll around", "wander", or an explicit "markov" request).
    # An ambient goal engages the semi-Markov pathing simulator instead of the
    # fixed observe/skim runner, so the scan motion differs every run.
    use_markov: bool = False


DEFAULT_STEPS: tuple[GoalStep, ...] = (
    GoalStep("visit_start_url", "visit"),
    GoalStep("settle_page", "wait"),
    GoalStep("inspect_visible_state", "read"),
    GoalStep("scroll_page", "scroll", required=False),
    GoalStep("record_visible_state", "record"),
)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _step_from_raw(raw: Any) -> GoalStep | None:
    if isinstance(raw, str):
        name = _safe_text(raw)
        return GoalStep(name=name, action=name) if name else None
    if not isinstance(raw, dict):
        return None
    name = _safe_text(raw.get("name") or raw.get("step") or raw.get("action"))
    if not name:
        return None
    action = _safe_text(raw.get("action")) or name
    params = raw.get("params") if isinstance(raw.get("params"), dict) else None
    return GoalStep(name=name, action=action, required=bool(raw.get("required", True)), params=params)


# --- Prompt -> interest / ambient signal extraction --------------------------
#
# These pure helpers turn ordinary language into the two structured signals the
# downstream goal+Markov hybrid needs: WHAT the operator cares about
# (``interest_terms``) and WHETHER the session is ambient enough to be driven by
# the semi-Markov pathing simulator (``use_markov``). They never touch a browser.

# Leading filler dropped from a captured interest phrase so "anything about
# hiring" yields "hiring", not "anything hiring".
_INTEREST_FILLER = frozenset(
    {
        "anything", "something", "any", "some", "posts", "post", "stuff",
        "things", "thing", "content", "updates", "update", "news", "more",
        "topics", "topic", "the", "a", "an", "",
    }
)

# Phrases that introduce a topic of interest. Multi-word triggers come first so
# "related to" wins over a bare "to".
_INTEREST_TRIGGERS: tuple[str, ...] = (
    "on the topic of", "related to", "to do with", "that mention",
    "interested in", "regarding", "concerning", "mentioning", "about",
)

# Markers that end the interest clause (the user moved on to another action).
_INTEREST_STOP_MARKERS: tuple[str, ...] = (
    " and then ", " then ", ", then", " after ", " before ", " so that ",
    " so i ", " and check", " and look", " and open", " and read",
)

# Signals that the session is ambient/aimless rather than a directed task. An
# ambient session is the natural home of the semi-Markov walk.
_AMBIENT_MARKERS: tuple[str, ...] = (
    "casual", "casually", "browse around", "browse like", "just browse",
    "just scroll", "scroll around", "poke around", "mess around", "look around",
    "wander", "aimless", "kill time", "killing time", "for a few minutes",
    "for a bit", "for a while", "like a user", "like a real user",
    "like a person", "like a human", "ambient", "leisurely", "random walk",
    "markov",
)


def _normalize_interest_terms(value: Any) -> tuple[str, ...]:
    """Coerce a stored ``interest_terms`` payload (list/tuple/comma string) into a
    clean tuple of lowercased terms."""
    if value is None:
        return ()
    parts: list[str] = []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        return ()
    out: list[str] = []
    for part in parts:
        term = " ".join(str(part).strip().lower().split())
        if term and term not in _INTEREST_FILLER and term not in out:
            out.append(term)
    return tuple(out)


def _extract_interest_terms(prompt: str) -> tuple[str, ...]:
    """Parse topical interest terms from a natural-language prompt.

    ``"scan my feed and read anything about hiring"`` -> ``("hiring",)``;
    ``"anything related to fundraising or new rounds"`` ->
    ``("fundraising", "new rounds")``. Returns ``()`` when the prompt declares no
    topic (an honest "no interest signal", not a fabricated default)."""
    low = _safe_text(prompt).lower()
    if not low:
        return ()
    tail = ""
    for trigger in _INTEREST_TRIGGERS:
        match = re.search(r"\b" + re.escape(trigger) + r"\s+(.+)$", low)
        if match:
            tail = match.group(1)
            break
    if not tail:
        return ()
    for marker in _INTEREST_STOP_MARKERS:
        idx = tail.find(marker)
        if idx != -1:
            tail = tail[:idx]
    raw_terms = re.split(r"\s+and\s+|\s+or\s+|,|/", tail)
    terms: list[str] = []
    for raw in raw_terms:
        words = [w for w in re.sub(r"[^a-z0-9\s\-]", " ", raw).split() if w]
        while words and words[0] in _INTEREST_FILLER:
            words.pop(0)
        while words and words[-1] in _INTEREST_FILLER:
            words.pop()
        term = " ".join(words[:5]).strip()
        if term and term not in _INTEREST_FILLER and term not in terms:
            terms.append(term)
    return tuple(terms)


def _implies_ambient_browsing(prompt_lower: str) -> bool:
    """True when the prompt reads as ambient/aimless browsing (Markov-appropriate)."""
    return any(marker in prompt_lower for marker in _AMBIENT_MARKERS)


def goal_spec_to_payload(goal: GoalSpec) -> dict[str, Any]:
    """Serialize a goal spec into a JSON-safe payload."""
    payload: dict[str, Any] = {
        "name": goal.name,
        "start_url": goal.start_url,
        "desired_outcome": goal.desired_outcome,
        "prompt": goal.prompt,
        "steps": [
            {
                "name": step.name,
                "action": step.action,
                "required": step.required,
                **({"params": step.params} if step.params else {}),
            }
            for step in goal.steps
        ],
    }
    if goal.interest_terms:
        payload["interest_terms"] = list(goal.interest_terms)
    if goal.use_markov:
        payload["use_markov"] = True
    return payload


def goal_spec_from_payload(raw_goal: Any, *, fallback_start_url: str = "") -> GoalSpec:
    """Build a goal spec from a JSON-safe goal payload."""
    if isinstance(raw_goal, str):
        try:
            raw_goal = json.loads(raw_goal)
        except json.JSONDecodeError:
            raw_goal = {"name": raw_goal}
    raw_goal = raw_goal if isinstance(raw_goal, dict) else {}
    raw_steps = raw_goal.get("steps") if isinstance(raw_goal.get("steps"), list) else []
    steps = tuple(step for step in (_step_from_raw(item) for item in raw_steps) if step)
    return GoalSpec(
        name=_safe_text(raw_goal.get("name") or raw_goal.get("goal") or DEFAULT_GOAL_NAME),
        start_url=_safe_text(raw_goal.get("start_url") or fallback_start_url),
        desired_outcome=_safe_text(raw_goal.get("desired_outcome") or DEFAULT_OUTCOME),
        steps=steps or DEFAULT_STEPS,
        prompt=_safe_text(raw_goal.get("prompt")),
        interest_terms=_normalize_interest_terms(raw_goal.get("interest_terms")),
        use_markov=bool(raw_goal.get("use_markov", False)),
    )


def goal_spec_from_target(target: dict[str, Any]) -> GoalSpec:
    """Build a goal spec from target configuration with safe defaults."""
    raw_goal = target.get("goal_spec") or target.get("automation_goal")
    if isinstance(raw_goal, str):
        try:
            raw_goal = json.loads(raw_goal)
        except json.JSONDecodeError:
            raw_goal = {"name": raw_goal}
    raw_goal = raw_goal if isinstance(raw_goal, dict) else {}

    payload = {**raw_goal, "prompt": raw_goal.get("prompt") or target.get("payload_prompt")}
    return goal_spec_from_payload(payload, fallback_start_url=_safe_text(target.get("entity_id")))


# --- Prompt interpretation pass (for agent control plane / user-declared prompts) ---

def goal_spec_from_natural_prompt(prompt: str, start_url: str = "", *, provider_hint: str | None = None) -> GoalSpec:
    """
    Basic pass to turn a natural-language user/agent prompt into a GoalSpec with discrete steps.

    This is the "interpret declared prompts about what they want into actions on the site" entry point.
    Full rich semantic interpretation (LLM/David/planner producing detailed steps + selectors from prompt + live page context)
    can be plugged in upstream and fed here (or directly construct GoalSpec).

    For now: simple keyword/rule-based + fallback to observation-with-prompt. Supports common action verbs so agents can
    drive real work (browse sheet, search, click results, type, etc.) through the humanized mechanics + variance.

    The resulting GoalSpec is then fed to build_behavior_plan (for all the persona/completion/outer variance, human twin)
    and the runner (which uses the redteam-improved primitives for execution).

    Static high-volume (LinkedIn etc.) bypass full interp for cost and use specialized paths, but can still attach the prompt
    for context and use behavior variance.
    """
    p = _safe_text(prompt).lower()
    name = _safe_text(prompt)[:80] or "user_prompted_task"
    url = _safe_text(start_url)

    # Structured signals parsed from the prompt and carried on every GoalSpec
    # branch below: WHAT the operator cares about (interest_terms) and WHETHER the
    # session is ambient enough to be driven by the semi-Markov walk (use_markov).
    interest_terms = _extract_interest_terms(prompt)
    ambient = _implies_ambient_browsing(p)

    steps: list[GoalStep] = [GoalStep("visit_start_url", "visit")]

    # --- LinkedIn site literacy ---------------------------------------------
    # When the target is LinkedIn, map common intents (feed, notifications,
    # messaging, people search) onto real LinkedIn affordances so the prompt
    # drives concrete nav/search actions instead of a blind generic scroll. The
    # feasibility review validates these selectors against the live DOM, and the
    # goal runner's LinkedIn awareness extracts structured posts on read steps.
    is_linkedin = (provider_hint or "").strip().lower() == "linkedin" or "linkedin.com" in url.lower()
    if is_linkedin:
        li_search_input = (
            "input[aria-label*='Search' i], .search-global-typeahead__input, "
            "input[placeholder*='Search' i]"
        )
        li_search_button = "button.search-global-typeahead__button, button[aria-label*='Search' i]"
        li_notifications = "a[href*='/notifications/']"
        li_messaging = "a[href*='/messaging/']"

        # Directed LinkedIn actions (notifications/messaging/search) run their
        # concrete steps; only the open-ended FEED scan engages the semi-Markov
        # walk (and only when the prompt reads as ambient browsing).
        li_use_markov = False

        if "notification" in p:
            steps.extend([
                GoalStep("open_notifications", "click", params={"selector": li_notifications}),
                GoalStep("settle_notifications", "wait"),
                GoalStep("read_notifications", "read"),
                GoalStep("record_visible_state", "record"),
            ])
            outcome = "prompt_executed"
        elif any(k in p for k in ("message", "messaging", "inbox", "dm")):
            steps.extend([
                GoalStep("open_messaging", "click", params={"selector": li_messaging}),
                GoalStep("settle_messaging", "wait"),
                GoalStep("read_messaging", "read"),
                GoalStep("record_visible_state", "record"),
            ])
            outcome = "prompt_executed"
        elif any(k in p for k in ("search", "find", "look for", "people", "connections", "profiles")):
            q = prompt
            for prefix in ("search for ", "look for ", "find ", "search "):
                if prefix in prompt.lower():
                    q = " ".join(prompt.split(prefix, 1)[1].strip().split()[0:8])
                    break
            steps.extend([
                GoalStep("focus_search", "click", params={"selector": li_search_input}),
                GoalStep("type_query", "type", params={"selector": li_search_input, "text": q}),
                GoalStep("submit_search", "click", required=False, params={"selector": li_search_button}),
                GoalStep("settle_results", "wait"),
                GoalStep("inspect_results", "read"),
                GoalStep("scroll_results", "scroll", required=False),
                GoalStep("record_visible_state", "record"),
            ])
            outcome = "prompt_executed"
        else:
            # Feed / scroll / browse / default: scroll the feed and extract posts.
            steps.extend([
                GoalStep("settle_page", "wait"),
                GoalStep("scroll_feed", "scroll", required=False),
                GoalStep("inspect_visible_state", "read"),
                GoalStep("record_visible_state", "record"),
            ])
            outcome = DEFAULT_OUTCOME
            # An ambient feed-browse prompt drives the scan with the semi-Markov
            # walk; a directed "gather posts" feed prompt stays on the extraction
            # runner (no regression to the proven structured-extraction path).
            li_use_markov = ambient

        return GoalSpec(
            name=name,
            start_url=url,
            desired_outcome=outcome,
            steps=tuple(steps),
            prompt=prompt,
            interest_terms=interest_terms,
            use_markov=li_use_markov,
        )

    # Check for click links pattern. Allow a leading "on" and common filler words
    # between the count and "links" so natural phrasings like "click 3 random
    # links", "click on three links", or "click a few different links" are honored
    # instead of degrading to a generic skim.
    click_links_match = re.search(
        r"click\s+(?:on\s+)?(\d+|five|four|three|two|one|a few|some|several)?\s*"
        r"(?:random|different|various|interesting)?\s*links?",
        p,
    )

    # Directed web flows (click N links / sandbox audit / search) execute their
    # concrete steps; only an OPEN-ENDED browse/observe session is eligible for
    # the semi-Markov walk, and only when the prompt reads as ambient.
    web_use_markov = False

    if click_links_match:
        num_str = click_links_match.group(1)
        num_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
        if not num_str:
            n_links = 3
        elif num_str.isdigit():
            n_links = int(num_str)
        else:
            n_links = num_map.get(num_str, 3)

        n_links = min(10, max(1, n_links))
        
        for i in range(1, n_links + 1):
            steps.append(GoalStep(f"click_random_link_{i}", "click", params={"selector": "random_link"}))
            steps.append(GoalStep(f"wait_after_click_{i}", "wait"))
            steps.append(GoalStep(f"scroll_new_page_{i}", "scroll", required=False))
            steps.append(GoalStep(f"wait_on_new_page_{i}", "wait"))
            steps.append(GoalStep(f"go_back_to_start_{i}", "backtrack"))
            steps.append(GoalStep(f"wait_after_back_{i}", "wait"))
            
    elif any(k in p for k in ("sandbox", "lhhl", "audit", "last human line")) or "sandbox" in url.lower():
        # Dedicated LHHL Sandbox Audit Flow
        steps.extend([
            GoalStep("settle_page", "wait"),
            GoalStep("click_sandbox", "click", params={"selector": "#sandbox"}),
            GoalStep("scroll_sandbox", "scroll", required=False),
            GoalStep("click_input", "click", params={"selector": "#text-input"}),
            GoalStep("type_text", "type", params={"selector": "#text-input", "text": "Imposter5 Evasion Test"}),
            GoalStep("click_submit", "click", params={"selector": "#submit-btn"}),
            GoalStep("wait_for_audit", "wait"),
            GoalStep("record_visible_state", "record")
        ])
    elif any(k in p for k in ("search", "find", "look for", "query", "google")):
        # Assume a search flow; selectors are illustrative / will be made robust in runner or by caller providing params
        steps.append(GoalStep("focus_search", "click", params={"selector": "input[type=search], input[name=q], textarea[title*='Search']"}))
        # Extract query heuristically
        q = prompt
        for prefix in ("search for ", "look for ", "find ", "query "):
            if prefix in prompt.lower():
                q = prompt.split(prefix, 1)[1].strip().split()[0:8]  # rough
                q = " ".join(q) if isinstance(q, list) else q
                break
        steps.append(GoalStep("type_query", "type", params={"selector": "input[type=search], input[name=q], textarea[title*='Search']", "text": q}))
        steps.append(GoalStep("submit_search", "click", params={"selector": "button[type=submit], input[type=submit], form button"}))
        steps.append(GoalStep("settle_results", "wait"))
        steps.append(GoalStep("inspect_results", "read", required=False))
        steps.append(GoalStep("scroll_results", "scroll", required=False))
    elif any(k in p for k in ("browse", "open", "sheet", "list", "get all", "extract", "people who", "begin with")):
        steps.append(GoalStep("settle_page", "wait"))
        steps.append(GoalStep("scroll_to_see", "scroll", required=False))
        steps.append(GoalStep("inspect_content", "read"))
        steps.append(GoalStep("record_extracted", "record"))
        web_use_markov = ambient
    else:
        # Generic observation-with-intent (current default behavior, now explicitly carrying the prompt for agent context)
        steps.extend([
            GoalStep("settle_page", "wait"),
            GoalStep("inspect_visible_state", "read"),
            GoalStep("scroll_page", "scroll", required=False),
            GoalStep("record_visible_state", "record"),
        ])
        web_use_markov = ambient

    return GoalSpec(
        name=name,
        start_url=url,
        desired_outcome="prompt_executed" if any(k in p for k in ("search", "click", "type", "browse", "extract", "link")) else DEFAULT_OUTCOME,
        steps=tuple(steps),
        prompt=prompt,
        interest_terms=interest_terms,
        use_markov=web_use_markov,
    )


def derive_plan_overrides(goal: GoalSpec) -> dict[str, Any]:
    """Translate a compiled prompt goal into behavior-plan overrides.

    This is the bridge that makes the prompt path actually USE the semi-Markov
    machinery instead of always falling through to the fixed observe/skim runner:

    - ``interest_terms`` are surfaced onto the plan so the LinkedIn goal+Markov
      hybrid's interest scoring (``linkedin_feed_scraper._resolve_interest_terms``)
      stops on what the user asked for rather than a generic default vocabulary.
    - ``use_markov`` flips ``use_markov_pathing`` so an ambient browsing prompt
      engages the dynamic semi-Markov walk (the same switch a literal "markov"
      prompt or an uploaded matrix already used) without the user naming the
      algorithm.
    - On a LinkedIn target the ambient walk is given the click/typing-free
      ``FEED_SCAN_MATRIX`` so the random walk only scans (scroll/hover/dwell) and
      never fires an arbitrary navigation — the goal layer still owns any opens.

    Pure: returns a dict of plan keys for the caller to merge; it never mutates a
    plan or touches a browser, so it is fully unit-testable.
    """
    overrides: dict[str, Any] = {}
    if goal.interest_terms:
        overrides["interest_terms"] = list(goal.interest_terms)
    if goal.use_markov:
        overrides["use_markov_pathing"] = True
        if "linkedin.com" in _safe_text(goal.start_url).lower():
            # Lazy import keeps this provider-neutral module free of a hard
            # dependency on the LinkedIn loader at import time.
            from imposter5.loaders.linkedin_feed_scraper import FEED_SCAN_MATRIX

            overrides["markov_matrix"] = FEED_SCAN_MATRIX
    return overrides
