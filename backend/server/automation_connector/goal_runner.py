"""Prompt-to-action goal runner for bounded browser observations."""
from __future__ import annotations

from typing import Any

from server.automation_connector.behavior_policy import planned_scroll_passes
from server.automation_connector.goals import GoalSpec
from server.automation_connector.interaction_primitives import (
    click_element,
    hover_element,
    maybe_backtrack,
    mobile_swipe,
    scroll_page,
    type_text,
    wait_human,
)
from server.automation_connector.session_recorder import SessionRecorder


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
    for action in compile_goal_actions(goal, behavior_plan):
        action_type = action["type"]
        if action_type == "goto":
            url = action.get("url") or goal.start_url
            page.goto(url, wait_until="domcontentloaded")
            recorder.record("goto", label="visit_start_url", metadata={"url": url})
        elif action_type == "wait":
            wait_ms = wait_human(page, behavior_plan, int(action.get("pass_index") or 0), 800, recorder=recorder)
            recorder.record("wait_complete", label="settle_page", metadata={"wait_ms": wait_ms})
        elif action_type == "scroll":
            delta_y = scroll_page(page, behavior_plan, int(action.get("pass_index") or 0), 900, recorder=recorder)
            recorder.record("scroll_complete", label="scroll_page", metadata={"delta_y": delta_y})
        elif action_type == "inspect_visible_state":
            visible_state = capture_visible_state(page)
            recorder.record(
                "inspect_visible_state",
                metadata={
                    "has_title": bool(visible_state["title"]),
                    "summary_chars": len(visible_state["summary"]),
                },
            )
        elif action_type == "record_visible_state":
            recorder.record("record_visible_state", metadata={"recorded": True})
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
        elif action_type == "mobile_swipe":
            mobile_swipe(page, behavior_plan, int(action.get("pass_index") or 0), recorder=recorder)
        else:
            recorder.record(action_type, status="skipped", metadata={"step": action.get("step")})
    return {
        **visible_state,
        "goal_actions": compile_goal_actions(goal, behavior_plan),
        "session_recording": recorder.payload(),
    }
