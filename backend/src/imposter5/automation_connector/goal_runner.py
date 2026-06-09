"""Prompt-to-action goal runner for bounded browser observations."""
from __future__ import annotations

import logging
from typing import Any

from imposter5.automation_connector.behavior_policy import planned_scroll_passes
from imposter5.automation_connector.goals import GoalSpec
from imposter5.automation_connector.interaction_primitives import (
    click_element,
    hover_element,
    maybe_backtrack,
    mobile_swipe,
    perceive_after_render,
    scroll_page,
    type_text,
    wait_human,
)
from imposter5.automation_connector.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)


def _is_linkedin_target(page: Any, goal: GoalSpec) -> bool:
    """True when the run is operating on LinkedIn (by goal URL or live page URL)."""
    parts: list[str] = []
    try:
        parts.append(str(getattr(goal, "start_url", "") or ""))
    except Exception:
        pass
    try:
        parts.append(str(page.url or ""))
    except Exception:
        pass
    return "linkedin.com" in " ".join(parts).lower()


def _linkedin_between_scroll(
    page: Any,
    behavior_plan: dict[str, Any] | None,
    recorder: SessionRecorder | None,
    *,
    variations: dict[str, Any],
    chances: dict[str, Any],
    sides_done: int,
    max_sides: int,
) -> int:
    """Run LinkedIn reading/side-trip behaviors between scrolls; returns new sides_done.

    Reuses the canned scraper's public adapter surface (hover-read, comment
    expand, notifications check, profile peek) so the organic path gets the same
    affordance-aware human micro-variations without duplicating the logic.
    """
    try:
        from imposter5.loaders import linkedin_feed_scraper as li
    except Exception:
        logger.debug("[goal_runner] linkedin adapter unavailable", exc_info=True)
        return sides_done

    # Always do the cheap, in-place reading variations (hover posts, expand comments).
    try:
        li.run_feed_reading_variations(
            page, behavior_plan, recorder, variations=variations, chances=chances
        )
    except Exception:
        logger.debug("[goal_runner] feed reading variations failed", exc_info=True)

    if sides_done >= max_sides:
        return sides_done

    # Bounded, optional off-goal side-trips (a curious human wanders).
    try:
        if variations.get("notifications_check") and li.visit_notifications(page, behavior_plan, recorder):
            return sides_done + 1
        if variations.get("profile_peeks") and li.peek_random_profile(page, behavior_plan, recorder):
            return sides_done + 1
    except Exception:
        logger.debug("[goal_runner] linkedin side-trip failed", exc_info=True)
    return sides_done


def compile_goal_actions(goal: GoalSpec, behavior_plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Compile a provider-neutral goal (observation or action from prompt) into a bounded action list.

    Supports the original observation steps + action steps for agent control plane use (click, type, hover, etc.).
    The behavior_plan (with its persona/completion/pointer variance) is threaded through so every execution
    gets the human-twin qualities (different "people" styles, partial completion, styled human moves, etc.).
    """
    actions: list[dict[str, Any]] = []
    scroll_passes = planned_scroll_passes(behavior_plan, 2)
    for step in goal.steps:
        params = step.params or {}
        if step.action == "visit":
            actions.append({"type": "goto", "url": goal.start_url or params.get("url", ""), "required": step.required})
        elif step.action == "wait":
            actions.append({"type": "wait", "pass_index": params.get("pass_index", 0), "required": step.required})
        elif step.action == "scroll":
            for pass_index in range(max(0, scroll_passes - 1)):
                actions.append({"type": "scroll", "pass_index": pass_index, "required": step.required})
                actions.append({"type": "wait", "pass_index": pass_index + 1, "required": False})
        elif step.action == "read":
            actions.append({"type": "inspect_visible_state", "required": step.required})
        elif step.action == "record":
            actions.append({"type": "record_visible_state", "required": step.required})
        elif step.action in ("click", "click_element"):
            actions.append({"type": "click", "selector": params.get("selector", step.name), "required": step.required})
        elif step.action in ("type", "type_text", "fill"):
            actions.append({
                "type": "type",
                "selector": params.get("selector", ""),
                "text": params.get("text", params.get("value", "")),
                "required": step.required,
            })
        elif step.action == "hover":
            actions.append({"type": "hover", "selector": params.get("selector", step.name), "required": step.required})
        elif step.action in ("back", "go_back", "backtrack"):
            actions.append({"type": "backtrack", "required": step.required})
        elif step.action == "swipe":
            actions.append({"type": "mobile_swipe", "pass_index": params.get("pass_index", 0), "required": step.required})
        else:
            actions.append({"type": "note_unsupported_step", "step": step.name, "required": step.required})
    return actions


def capture_visible_state(page: Any, *, max_chars: int = 600) -> dict[str, str]:
    """Capture a compact visible-state payload from the current page."""
    title = str(page.title() or "").strip()
    body_text = str(page.inner_text("body") or "").strip()
    excerpt = " ".join(body_text.split())[:max_chars]
    return {"title": title, "summary": excerpt}


def run_visible_state_goal(
    page: Any,
    goal: GoalSpec,
    behavior_plan: dict[str, Any],
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Run a bounded goal (observation or action) against an already-open page.

    Dispatches to the full set of humanized interaction_primitives (type, click with styled move_pointer,
    hover, scroll, waits, backtrack, mobile, etc.). The behavior_plan ensures every execution gets
    persona-driven variance, completion levels (sometimes partial), human ergonomics, and the redteam
    mouse/click physics — so agent prompt executions or campaign repetitions look like a careful human
    (good digital twin), not rigid scripted BOP.
    """
    recorder = recorder or SessionRecorder(behavior_plan)
    visible_state: dict[str, str] = {"title": "", "summary": ""}

    # LinkedIn literacy: when the run is on LinkedIn, read steps extract structured
    # posts (not just a text blob) and scrolls trigger affordance-aware human
    # reading + bounded side-trips, reusing the canned scraper's adapter surface.
    linkedin = _is_linkedin_target(page, goal)
    variations = (behavior_plan or {}).get("variations") or {}
    chances = (behavior_plan or {}).get("variation_chances") or {}
    max_sides = int(variations.get("max_side_actions", 2 if linkedin else 0))
    sides_done = 0
    linkedin_posts: list[dict] = []

    def _extract_linkedin_posts() -> None:
        nonlocal linkedin_posts
        try:
            from imposter5.loaders.linkedin_feed_scraper import extract_visible_posts, merge_unique_posts
            linkedin_posts = merge_unique_posts(linkedin_posts, extract_visible_posts(page))
        except Exception:
            logger.debug("[goal_runner] linkedin extraction failed", exc_info=True)

    for action in compile_goal_actions(goal, behavior_plan):
        action_type = action["type"]
        if action_type == "goto":
            url = action.get("url") or goal.start_url
            page.goto(url, wait_until="domcontentloaded")
            recorder.record("goto", label="visit_start_url", metadata={"url": url})
            # New view rendered: take it in before the first action (no instant reaction).
            perceive_after_render(page, behavior_plan, recorder=recorder)
        elif action_type == "wait":
            wait_ms = wait_human(page, behavior_plan, int(action.get("pass_index") or 0), 800, recorder=recorder)
            recorder.record("wait_complete", label="settle_page", metadata={"wait_ms": wait_ms})
        elif action_type == "scroll":
            delta_y = scroll_page(page, behavior_plan, int(action.get("pass_index") or 0), 900, recorder=recorder)
            recorder.record("scroll_complete", label="scroll_page", metadata={"delta_y": delta_y})
            if linkedin:
                sides_done = _linkedin_between_scroll(
                    page, behavior_plan, recorder,
                    variations=variations, chances=chances,
                    sides_done=sides_done, max_sides=max_sides,
                )
                _extract_linkedin_posts()
        elif action_type == "inspect_visible_state":
            visible_state = capture_visible_state(page)
            if linkedin:
                _extract_linkedin_posts()
            recorder.record(
                "inspect_visible_state",
                metadata={
                    "has_title": bool(visible_state["title"]),
                    "summary_chars": len(visible_state["summary"]),
                    "linkedin_posts": len(linkedin_posts),
                },
            )
        elif action_type == "record_visible_state":
            if linkedin:
                _extract_linkedin_posts()
            recorder.record("record_visible_state", metadata={"recorded": True, "linkedin_posts": len(linkedin_posts)})
        elif action_type == "click":
            sel = action.get("selector") or ""
            if sel:
                click_element(page, sel, behavior_plan, recorder=recorder)
            recorder.record("click", metadata={"selector": sel})
        elif action_type == "type":
            sel = action.get("selector") or ""
            txt = action.get("text") or ""
            if sel and txt:
                type_text(page, sel, txt, behavior_plan, recorder=recorder)
            recorder.record("type_text", metadata={"selector": sel, "text": txt})
        elif action_type == "hover":
            sel = action.get("selector") or ""
            if sel:
                hover_element(page, sel, behavior_plan, recorder=recorder)
            recorder.record("hover", metadata={"selector": sel})
        elif action_type == "backtrack":
            maybe_backtrack(page, behavior_plan, recorder=recorder)
            # Back-navigation re-renders a prior view: perceive it before acting.
            perceive_after_render(page, behavior_plan, recorder=recorder)
        elif action_type == "mobile_swipe":
            mobile_swipe(page, behavior_plan, int(action.get("pass_index") or 0), recorder=recorder)
        else:
            recorder.record(action_type, status="skipped", metadata={"step": action.get("step")})
    return {
        **visible_state,
        "goal_actions": compile_goal_actions(goal, behavior_plan),
        "linkedin_posts": linkedin_posts,
        "session_recording": recorder.payload(),
    }
