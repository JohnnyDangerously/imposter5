"""Pre-execution feasibility / action review (pipeline seam — workstream C fills in).

Contract used by the ``/api/imposter5/run`` pipeline, called with the browser
already open on the target page (so the DOM can be inspected) but BEFORE the
compiled goal is executed:

    report = review_feasibility(page, goal, plan)

``report`` is a :class:`FeasibilityReport`. The pipeline reads it:

- ``status == "ok"``        -> every required step has a resolvable target; run.
- ``status == "skipped"``   -> review not performed; run (current stub behavior).
- ``status == "infeasible"``-> at least one REQUIRED step cannot be performed on
  this page (e.g. user asked to click "Messages" but no such affordance exists);
  SHORT-CIRCUIT and return ``to_payload()`` so the UI can tell the user which
  steps are not possible and why, instead of silently clicking a fallback spot.

This stub returns ``skipped`` so nothing is blocked until workstream C implements
the real dry-run using ``story.site_mapper.SiteMapper`` to resolve each compiled
``GoalStep``'s target against the live affordance map.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class StepFeasibility:
    """Per-step verdict from the action review."""

    step: str
    action: str
    feasible: bool
    required: bool = True
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action": self.action,
            "feasible": self.feasible,
            "required": self.required,
            "reason": self.reason,
        }


@dataclass
class FeasibilityReport:
    """Outcome of reviewing a compiled goal against the live page."""

    status: str = "skipped"  # "ok" | "infeasible" | "skipped"
    steps: list[StepFeasibility] = field(default_factory=list)
    summary: str = ""

    @property
    def blocks_run(self) -> bool:
        return self.status == "infeasible"

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "steps": [s.to_payload() for s in self.steps],
            "blocks_run": self.blocks_run,
        }


# --- step -> target resolution helpers -------------------------------------------
#
# A compiled ``GoalStep`` (see ``automation_connector.goals``) carries an ``action``
# and optional ``params`` (typically a ``selector``, and for typed targets a textual
# ``label``). Only a subset of actions touch a concrete element and can therefore be
# *impossible* on a given page; the rest (visit/wait/read/scroll/record/backtrack)
# need no live affordance and are always feasible.

# Actions that must resolve to a concrete, usable element on the live page.
_TARGETED_ACTIONS = frozenset(
    {"click", "type", "fill", "select", "select_option", "press", "hover", "tap", "check", "upload"}
)

# Compiler sentinels that are not real CSS selectors. They name an *intent* ("pick a
# random link") rather than a literal element, so we translate them to a generic
# affordance probe instead of querying the literal string as a tag selector.
_SENTINEL_SELECTORS = {
    "random_link": "a[href], [role=link]",
}

# Substrings in a step's name/action/selector that hint which SiteMapper affordance
# role should back the step. Used for semantic ("is there a search box?") resolution
# in addition to any literal selector the compiler attached.
_ROLE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("search", ("search_input",)),
    ("query", ("search_input",)),
    ("submit", ("search_submit",)),
    ("result", ("result_open", "result_item")),
    ("link", ("result_open", "nav_target")),
    ("nav", ("nav_target",)),
    ("menu", ("nav_target",)),
    ("back", ("back_control",)),
    ("profile", ("profile_view",)),
)

_NO_TARGET_REASON = "no concrete page target required"


def _selector_text(step: Any) -> str:
    params = getattr(step, "params", None) or {}
    raw = params.get("selector") or params.get("target") or ""
    return str(raw or "").strip()


def _label_target(step: Any) -> str:
    """Human-readable target text (e.g. ``label="Messages"``), if the step names one.

    Deliberately does NOT read ``params['text']`` — for a ``type`` step that is the
    text being typed, not the element to find.
    """
    params = getattr(step, "params", None) or {}
    for key in ("label", "target_text", "link_text", "text_target"):
        val = params.get(key)
        if val:
            return str(val).strip()
    return ""


def _candidate_roles(step: Any) -> list[str]:
    hay = f"{getattr(step, 'name', '')} {getattr(step, 'action', '')} {_selector_text(step)}".lower()
    roles: list[str] = []
    for needle, role_group in _ROLE_HINTS:
        if needle in hay:
            for role in role_group:
                if role not in roles:
                    roles.append(role)
    return roles


def _element_text(element: Any) -> str:
    for attr in ("inner_text", "text_content"):
        getter = getattr(element, attr, None)
        if callable(getter):
            try:
                val = getter()
            except Exception:
                continue
            if val:
                return str(val)
    get_attribute = getattr(element, "get_attribute", None)
    if callable(get_attribute):
        for name in ("aria-label", "title"):
            try:
                val = get_attribute(name)
            except Exception:
                continue
            if val:
                return str(val)
    return ""


def _probe_selector(page: Any, selector: str) -> bool | None:
    """Tri-state probe of a literal selector against the live DOM.

    Returns ``True`` when at least one matching element is present and visible,
    ``False`` when the DOM has zero matches (a confident "not here"), and ``None``
    when the result is inconclusive (selector unusable, matches present but none
    assessable as visible). ``None`` never blocks a run.
    """
    sel = selector.strip()
    if not sel:
        return None
    translated = _SENTINEL_SELECTORS.get(sel.lower(), sel)
    try:
        locator = page.locator(translated)
    except Exception:
        return None
    matches: list[Any] | None
    try:
        matches = list(locator.all())
    except Exception:
        matches = None
    if matches is None:
        try:
            count = int(locator.count())
        except Exception:
            return None
        return count > 0
    if not matches:
        return False
    saw_match = False
    for element in matches[:60]:
        saw_match = True
        try:
            if element.is_visible():
                return True
        except Exception:
            # Cannot assess visibility -> stay conservative, do not block.
            return True
    # Matches exist but none assessed visible (e.g. honeypot-only): inconclusive.
    return None if saw_match else False


def _label_resolves(mapper: Any, roles: list[str], label: str) -> bool | None:
    """Whether an affordance bearing ``label`` exists among ``roles``.

    ``True``  -> an element in one of the roles contains the label text.
    ``False`` -> the roles resolve to elements, but none bears that text (the
                 "you asked to click Messages but there is no Messages" case).
    ``None``  -> no elements resolved for these roles, so we cannot decide.
    """
    needle = label.strip().lower()
    if not needle:
        return None
    saw_any = False
    for role in roles:
        try:
            elements = mapper.resolve_all(role)
        except Exception:
            elements = []
        for element in elements:
            saw_any = True
            text = _element_text(element)
            if text and needle in text.lower():
                return True
    return False if saw_any else None


def _evaluate_step(page: Any, mapper: Any, amap: Any, step: Any) -> StepFeasibility:
    name = str(getattr(step, "name", "") or "step")
    action = str(getattr(step, "action", "") or "").strip().lower()
    required = bool(getattr(step, "required", True))

    if action not in _TARGETED_ACTIONS:
        return StepFeasibility(name, action or "?", feasible=True, required=required, reason=_NO_TARGET_REASON)

    roles = _candidate_roles(step)
    role_present = any(amap.counts.get(role, 0) > 0 for role in roles)
    label = _label_target(step)

    # A named target ("Messages") must actually exist among the semantic affordances.
    if label and roles:
        label_state = _label_resolves(mapper, roles, label)
        if label_state is True:
            return StepFeasibility(name, action, feasible=True, required=required,
                                   reason=f"found '{label}' affordance on this page")
        if label_state is False:
            return StepFeasibility(name, action, feasible=False, required=required,
                                   reason=f"no '{label}' affordance found on this page")
        # label_state is None: roles did not resolve; fall through to generic checks.

    selector = _selector_text(step)
    probe = _probe_selector(page, selector) if selector else None

    if probe is True:
        return StepFeasibility(name, action, feasible=True, required=required,
                               reason=f"target selector '{selector}' resolved on this page")
    if role_present:
        return StepFeasibility(name, action, feasible=True, required=required,
                               reason=f"matching affordance ({', '.join(roles)}) present on this page")
    if probe is False:
        target_desc = f"'{selector}'" if selector else "the required target"
        return StepFeasibility(name, action, feasible=False, required=required,
                               reason=f"no element matching {target_desc} (and no matching affordance) on this page")

    # Inconclusive (non-CSS sentinel, unusable selector, or no hints): be conservative
    # and allow the run rather than false-block a possibly valid task.
    return StepFeasibility(name, action, feasible=True, required=required,
                           reason="target not conclusively resolvable; allowing run (conservative)")


def review_feasibility(page: Any, goal: Any, plan: dict[str, Any] | None = None) -> FeasibilityReport:
    """Dry-run a compiled goal against the live DOM and report can/can't per step.

    Resolves every REQUIRED step's target against the live affordance map built by
    :class:`imposter5.story.site_mapper.SiteMapper`:

    - actions that touch no concrete element (visit/wait/read/scroll/record/...) are
      always feasible;
    - targeted actions (click/type/...) must resolve either to their literal selector
      on the page or to the matching SiteMapper affordance role (and, when a step
      names a target like ``Messages``, an affordance actually bearing that text);
    - a required step with a confident "no such target" makes the whole report
      ``infeasible`` and short-circuits the run.

    Conservative by design: anything we cannot evaluate is treated as feasible, so the
    review catches clearly-impossible tasks without false-blocking valid ones. If the
    page itself cannot be mapped, the report is ``skipped`` (does not block).
    """
    try:
        steps = tuple(getattr(goal, "steps", ()) or ())
    except Exception:
        steps = ()
    if not steps:
        return FeasibilityReport(status="ok", steps=[], summary="no compiled steps to review")

    from imposter5.story.site_mapper import SiteMapper

    try:
        mapper = SiteMapper(page)
        amap = mapper.map_view(refresh=True)
    except Exception:
        logger.exception("[feasibility] could not map page affordances; skipping review")
        return FeasibilityReport(status="skipped", steps=[],
                                 summary="could not map page affordances; feasibility review skipped")

    verdicts = [_evaluate_step(page, mapper, amap, step) for step in steps]
    blocking = [v for v in verdicts if v.required and not v.feasible]
    targeted = [v for v in verdicts if v.reason != _NO_TARGET_REASON]

    if blocking:
        names = ", ".join(f"'{v.step}'" for v in blocking)
        summary = (
            f"{len(blocking)} required step(s) cannot be performed on this page: {names}"
        )
        return FeasibilityReport(status="infeasible", steps=verdicts, summary=summary)

    summary = (
        f"all {len(targeted)} targeted step(s) resolved against the live page"
        if targeted
        else "no targeted steps required; nothing to block"
    )
    return FeasibilityReport(status="ok", steps=verdicts, summary=summary)
