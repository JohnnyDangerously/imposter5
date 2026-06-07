"""Journey Executor: walk a sampled StoryPlan against a LIVE page.

The executor is the goal/intent brain only. It NEVER reimplements motion: every
physical action is delegated to the existing analog motor primitives
(``move_pointer`` / ``type_text`` / ``scroll_page`` / ``hover_element`` /
``click_element``) and to the semi-Markov micro-behavior engine
(``run_markov_simulation``) for ambient reading/scanning dwell. All randomness is
drawn from the single advancing per-session RNG so the motion is continuous and
never teleports, and honeypots are avoided by the SiteMapper's reuse of the
existing honeypot engine.

Curiosity/wander is realized here as a RESUME STACK: when a tangent scene that
navigates away fires, the executor pushes the interrupted location; the tangent's
RETURN edge pops it and navigates back, so the agent always resumes and completes
the main goal.
"""
from __future__ import annotations

import logging
from typing import Any

from imposter5.automation_connector.behavior_policy import build_behavior_plan
from imposter5.automation_connector.humanize_dist import lognormal_ms, scroll_decay_deltas
from imposter5.automation_connector.interaction_primitives import (
    _session_rng,
    click_element,
    hover_element,
    move_pointer,
    scroll_page,
    type_text,
    update_status_ticker,
)
from imposter5.automation_connector.session_recorder import SessionRecorder
from imposter5.loaders.markov_simulator import run_markov_simulation
from imposter5.story.compiler import Scene, StoryPlan, compile_story
from imposter5.story.goal import GoalChecker, GoalState
from imposter5.story.site_mapper import SiteMapper
from imposter5.story.task_intent import TaskIntent

logger = logging.getLogger(__name__)

# Ambient micro-behavior matrices for DRIVING the semi-Markov engine during reading
# and scanning WITHOUT navigating away: click/typing are near-zero so the burst only
# produces idle/mousemove/scroll/hover (continuous analog motion, no stray clicks).
_READING_MATRIX = {
    "idle": {"idle": 0.40, "mousemove": 0.20, "scroll_down": 0.22, "scroll_up": 0.16, "hover": 0.02, "click": 0.001, "typing": 0.001},
    "mousemove": {"idle": 0.40, "mousemove": 0.20, "scroll_down": 0.20, "scroll_up": 0.12, "hover": 0.08, "click": 0.001, "typing": 0.001},
    "scroll_down": {"idle": 0.45, "mousemove": 0.18, "scroll_down": 0.22, "scroll_up": 0.10, "hover": 0.05, "click": 0.001, "typing": 0.001},
    "scroll_up": {"idle": 0.45, "mousemove": 0.20, "scroll_down": 0.14, "scroll_up": 0.16, "hover": 0.05, "click": 0.001, "typing": 0.001},
    "hover": {"idle": 0.45, "mousemove": 0.25, "scroll_down": 0.15, "scroll_up": 0.08, "hover": 0.07, "click": 0.001, "typing": 0.001},
    "click": {"idle": 0.50, "mousemove": 0.25, "scroll_down": 0.15, "scroll_up": 0.05, "hover": 0.05, "click": 0.001, "typing": 0.001},
    "typing": {"idle": 0.50, "mousemove": 0.25, "scroll_down": 0.15, "scroll_up": 0.05, "hover": 0.05, "click": 0.001, "typing": 0.001},
}
_SCANNING_MATRIX = {
    "idle": {"idle": 0.18, "mousemove": 0.27, "scroll_down": 0.38, "scroll_up": 0.06, "hover": 0.10, "click": 0.001, "typing": 0.001},
    "mousemove": {"idle": 0.16, "mousemove": 0.24, "scroll_down": 0.36, "scroll_up": 0.06, "hover": 0.17, "click": 0.001, "typing": 0.001},
    "scroll_down": {"idle": 0.20, "mousemove": 0.22, "scroll_down": 0.40, "scroll_up": 0.06, "hover": 0.11, "click": 0.001, "typing": 0.001},
    "scroll_up": {"idle": 0.22, "mousemove": 0.24, "scroll_down": 0.30, "scroll_up": 0.12, "hover": 0.11, "click": 0.001, "typing": 0.001},
    "hover": {"idle": 0.20, "mousemove": 0.26, "scroll_down": 0.32, "scroll_up": 0.05, "hover": 0.16, "click": 0.001, "typing": 0.001},
    "click": {"idle": 0.25, "mousemove": 0.25, "scroll_down": 0.34, "scroll_up": 0.05, "hover": 0.10, "click": 0.001, "typing": 0.001},
    "typing": {"idle": 0.25, "mousemove": 0.25, "scroll_down": 0.34, "scroll_up": 0.05, "hover": 0.10, "click": 0.001, "typing": 0.001},
}

# Off-goal queries used by curiosity research tangents (deliberately unrelated to the
# objective query so the excursion is off-goal by construction).
_OFFGOAL_QUERIES = (
    "weekend hiking trips",
    "best espresso machine",
    "old colleague",
    "conference talks 2026",
    "guitar lessons",
)


class StoryExecutor:
    def __init__(
        self,
        page: Any,
        plan: StoryPlan,
        behavior_plan: dict[str, Any],
        *,
        recorder: SessionRecorder | None = None,
        dwell_scale: float = 1.0,
        ambient_steps: int = 5,
        max_scan_passes: int = 14,
        speed_scale: float = 1.0,
    ) -> None:
        self.page = page
        self.plan = plan
        self.behavior_plan = behavior_plan
        self.recorder = recorder or SessionRecorder(behavior_plan)
        self.dwell_scale = max(0.0, float(dwell_scale))
        self.ambient_steps = max(0, int(ambient_steps))
        self.max_scan_passes = max(1, int(max_scan_passes))
        # speed_scale < 1.0 compresses ALL motor sleeps (test-only fast path). The
        # NUMBER of move/scroll micro-steps is unchanged, so motion stays continuous
        # and multi-step; only the per-step dwell shrinks. Production keeps 1.0.
        self.speed_scale = max(0.0, float(speed_scale))
        if self.speed_scale < 1.0:
            self._install_fast_clock()

        self.mapper = SiteMapper(page)
        self.rng = _session_rng(page, behavior_plan)
        self.goal = GoalChecker(plan.goal_predicate, self.rng)
        self.state = GoalState()

        self._resume_stack: list[dict[str, Any]] = []
        self._opened_objective_keys: set[str] = set()
        self.trace: list[dict[str, Any]] = []
        self.tangents_fired = 0
        self.tangents_returned = 0
        self.profiles_opened_total = 0  # objective + off-goal tangent opens
        self._goal_met = False
        # Monotonic scan progress in [0, 1]; only ever advanced by real scroll geometry.
        self._scan_progress = 0.0

    def _install_fast_clock(self) -> None:
        """Wrap ``page.wait_for_timeout`` so every motor sleep is scaled down.

        Test-only: invoked when ``speed_scale < 1.0``. Idempotent and best-effort;
        if the page rejects attribute assignment we simply keep real timing.
        """
        page = self.page
        if getattr(page, "_story_fast_clock", False):
            return
        orig = getattr(page, "wait_for_timeout", None)
        if not callable(orig):
            return
        scale = self.speed_scale

        def _fast(ms: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                ms = max(0, int(float(ms) * scale))
            except (TypeError, ValueError):
                pass
            return orig(ms, *args, **kwargs)

        try:
            page.wait_for_timeout = _fast  # type: ignore[assignment]
            page._story_fast_clock = True
        except Exception:
            logger.debug("[story] could not install fast clock", exc_info=True)

    # --- helpers ------------------------------------------------------------------
    def _dwell(self, scene: Scene) -> None:
        ms = int(scene.dwell_ms * self.dwell_scale)
        if ms > 0:
            try:
                self.page.wait_for_timeout(ms)
            except Exception:
                logger.debug("[story] wait_for_timeout failed", exc_info=True)

    def _ambient(self, matrix: dict[str, Any], steps: int | None = None) -> None:
        """Drive the semi-Markov engine for ambient reading/scanning (no navigation)."""
        n = self.ambient_steps if steps is None else steps
        if n <= 0:
            return
        burst_plan = dict(self.behavior_plan)
        burst_plan["markov_matrix"] = matrix
        try:
            run_markov_simulation(self.page, burst_plan, recorder=self.recorder, max_steps=n)
        except Exception:
            logger.debug("[story] ambient markov burst failed", exc_info=True)

    def _element_key(self, locator: Any) -> str:
        """A stable-ish per-element key (no site-specific id assumption required)."""
        for attr in ("data-person-id", "data-id", "href", "id"):
            try:
                val = locator.get_attribute(attr)
            except Exception:
                val = None
            if val:
                return f"{attr}={val}"
        try:
            return f"text={(locator.inner_text() or '')[:40]}"
        except Exception:
            return f"obj={id(locator)}"

    def _move_to(self, locator: Any) -> bool:
        """Continuous analog move onto an element's center (no teleport)."""
        try:
            box = locator.bounding_box()
        except Exception:
            box = None
        if not box:
            return False
        cx = box["x"] + box["width"] * self.rng.uniform(0.3, 0.7)
        cy = box["y"] + box["height"] * self.rng.uniform(0.3, 0.7)
        move_pointer(
            self.page, cx, cy, self.behavior_plan, recorder=self.recorder,
            target_w=box.get("width"), target_h=box.get("height"),
        )
        return True

    def _recount_results(self) -> None:
        items = self.mapper.resolve_all("result_item")
        if items:
            self.state.results_total = max(self.state.results_total, len(items))

    # Single round-trip geometry probe. Reports the element's OWN scroll geometry and,
    # as a fallback, the WINDOW scroll geometry, so one cheap call covers both an inner
    # overflow container and a window-scrolled list.
    _SCAN_GEOMETRY_JS = r"""
    (el) => {
        const doc = document.scrollingElement || document.documentElement;
        const win = {st: doc.scrollTop, ch: window.innerHeight, sh: doc.scrollHeight};
        if (!el) return {el: null, win};
        return {el: {st: el.scrollTop, ch: el.clientHeight, sh: el.scrollHeight}, win};
    }
    """

    @staticmethod
    def _frac_from(geom: dict[str, float] | None) -> float | None:
        if not geom:
            return None
        sh = float(geom.get("sh", 0) or 0)
        ch = float(geom.get("ch", 0) or 0)
        if sh <= ch + 4:  # not its own scroll region
            return None
        return max(0.0, min(1.0, (float(geom.get("st", 0)) + ch) / (sh or 1.0)))

    def _measure_scan_progress(self, list_el: Any) -> float:
        """Measure scan progress in [0, 1], robust to the list's scroll model.

        ONE round-trip reads the resolved list's own scroll geometry AND the window's:
        - INNER OVERFLOW CONTAINER (results pane with its own scrollbar):
          progress = (scrollTop + clientHeight) / scrollHeight, reaching 1.0 at bottom.
        - WINDOW-SCROLLED LIST (real LinkedIn): the list isn't its own scroll region,
          so fall back to the window's (scrollTop + innerHeight) / scrollHeight.

        Progress is tracked MONOTONICALLY; we only ever read genuine scroll geometry,
        never fabricated progress.
        """
        geom: dict[str, Any] | None = None
        try:
            target = list_el.first if (list_el is not None and hasattr(list_el, "first")) else list_el
            page_eval = target.evaluate if target is not None else None
            if page_eval is not None:
                geom = target.evaluate(self._SCAN_GEOMETRY_JS)
            else:
                geom = self.page.evaluate(self._SCAN_GEOMETRY_JS, None)
        except Exception:
            geom = None
        frac = None
        if isinstance(geom, dict):
            frac = self._frac_from(geom.get("el"))
            if frac is None:
                frac = self._frac_from(geom.get("win"))
        if frac is None:
            return self._scan_progress
        self._scan_progress = max(self._scan_progress, frac)
        return self._scan_progress

    def _update_scan_state(self, list_el: Any) -> None:
        """Translate measured progress into the monotonic GoalState scan counter."""
        progress = self._measure_scan_progress(list_el)
        total = max(1, self.state.results_total)
        self.state.results_scanned = max(self.state.results_scanned, round(progress * total))

    # --- scene handlers -----------------------------------------------------------
    def _do_search_open(self, scene: Scene) -> dict[str, Any]:
        el = self.mapper.resolve_one("search_input")
        if el is None:
            return {"status": "no_search_input"}
        self._move_to(el)
        try:
            el.click()
        except Exception:
            logger.debug("[story] search focus click failed", exc_info=True)
        return {"status": "ok"}

    def _do_search_query(self, scene: Scene, query: str | None = None) -> dict[str, Any]:
        q = query if query is not None else (self.plan.query_hint or "")
        el = self.mapper.resolve_one("search_input")
        if el is None:
            return {"status": "no_search_input"}
        try:
            type_text(self.page, el, q, self.behavior_plan, recorder=self.recorder)
        except Exception:
            logger.debug("[story] type query failed", exc_info=True)
            return {"status": "type_failed"}
        submit = self.mapper.resolve_one("search_submit")
        submitted = "enter"
        if submit is not None:
            self._move_to(submit)
            try:
                submit.click()
                submitted = "button"
            except Exception:
                submitted = "enter"
        if submitted == "enter":
            try:
                el.press("Enter")
            except Exception:
                logger.debug("[story] enter submit failed", exc_info=True)
        self._settle()
        self._recount_results()
        return {"status": "ok", "query": q, "submitted_via": submitted, "results_total": self.state.results_total}

    def _human_wheel_scroll(self, list_el: Any, total_px: int) -> bool:
        """Analog wheel scroll that reliably targets the resolved list.

        The wheel only scrolls the element under the REAL Playwright cursor, so we (1)
        move there with the continuous analog motor (recorded, no teleport) and (2)
        guarantee the real cursor sits inside the list before wheeling. The wheel
        burst itself reuses the existing scroll-decay + log-normal step timing so the
        momentum bleed-off stays human. Works for an inner overflow container AND a
        window-scrolled list (cursor is over the list content either way).
        """
        cx = cy = None
        if list_el is not None:
            try:
                box = list_el.bounding_box()
            except Exception:
                box = None
            if box:
                cx = box["x"] + box["width"] * self.rng.uniform(0.35, 0.65)
                cy = box["y"] + box["height"] * self.rng.uniform(0.35, 0.65)
                move_pointer(self.page, cx, cy, self.behavior_plan, recorder=self.recorder,
                             target_w=box.get("width"), target_h=box.get("height"))
        if cx is None:
            # No resolvable list: fall back to the generic scroll primitive.
            scroll_page(self.page, self.behavior_plan, fallback_delta_y=total_px, recorder=self.recorder)
            return False
        try:
            self.page.mouse.move(cx, cy)  # ensure the wheel targets THIS container
        except Exception:
            logger.debug("[story] real cursor move before wheel failed", exc_info=True)
        deltas = scroll_decay_deltas(self.rng, total_px=float(total_px), max_steps=8, decay=0.6)
        for d in deltas:
            try:
                self.page.mouse.wheel(0, d)
            except Exception:
                break
            self.page.wait_for_timeout(int(lognormal_ms(self.rng, mean_ms=55.0, cv=0.4, lo=4.0, hi=400.0)))
        self.page.wait_for_timeout(int(lognormal_ms(self.rng, mean_ms=220.0, cv=0.4, lo=20.0, hi=1500.0)))
        if self.recorder is not None:
            self.recorder.record("scroll", metadata={"delta_y": total_px, "steps": len(deltas), "via": "story_wheel"})
        return True

    def _do_results_scan(self, scene: Scene) -> dict[str, Any]:
        self._recount_results()
        # Ambient scanning micro-behavior via the semi-Markov engine (no clicks).
        self._ambient(_SCANNING_MATRIX)
        list_el = self.mapper.resolve_one("result_list")
        self._update_scan_state(list_el)  # baseline before scrolling
        passes = 0
        stalls = 0
        while passes < self.max_scan_passes and not self.goal.is_satisfied(self.state):
            passes += 1
            before = self.state.results_scanned
            self._human_wheel_scroll(list_el, self.rng.randint(360, 720))
            # Hover a visible result occasionally to look human while scanning.
            if self.rng.random() < 0.5:
                items = self.mapper.resolve_all("result_item")
                if items:
                    tgt = self.rng.choice(items[: min(len(items), 8)])
                    try:
                        hover_element(self.page, tgt, self.behavior_plan, recorder=self.recorder)
                    except Exception:
                        logger.debug("[story] scan hover failed", exc_info=True)
            self._update_scan_state(list_el)
            # Stop early if scrolling no longer advances (reached the bottom).
            if self.state.results_scanned <= before:
                stalls += 1
                if stalls >= 2:
                    break
            else:
                stalls = 0
        return {
            "status": "ok",
            "passes": passes,
            "results_scanned": self.state.results_scanned,
            "results_total": self.state.results_total,
            "scan_fraction": round(self.state.scan_fraction, 3),
            "scan_progress": round(self._scan_progress, 3),
        }

    def _open_profile(self, *, objective: bool) -> dict[str, Any]:
        """Click a result's open-link to enter a profile view.

        ``objective`` True opens a not-yet-opened goal item (advances goal counters);
        False opens a NON-objective item (off-goal wander; no goal advance).
        """
        opens = self.mapper.resolve_all("result_open")
        if not opens:
            return {"status": "no_result_open"}
        chosen = None
        if objective:
            for el in opens:
                if self._element_key(el) not in self._opened_objective_keys:
                    chosen = el
                    break
            chosen = chosen or self.rng.choice(opens)
            self._opened_objective_keys.add(self._element_key(chosen))
        else:
            # Off-goal: prefer an item NOT already an objective target.
            offgoal = [el for el in opens if self._element_key(el) not in self._opened_objective_keys]
            chosen = self.rng.choice(offgoal or opens)
        key = self._element_key(chosen)
        try:
            click_element(self.page, chosen, self.behavior_plan, recorder=self.recorder)
        except Exception:
            logger.debug("[story] profile open click failed", exc_info=True)
            return {"status": "open_failed"}
        self.profiles_opened_total += 1
        self._settle()
        return {"status": "ok", "objective": objective, "element": key}

    def _read_profile(self) -> dict[str, Any]:
        # Reading micro-behavior via the semi-Markov engine (scroll/idle/hover, no nav).
        # Honor the ambient_steps knob (tests pass a small value for speed); production
        # uses the default of 5, which yields a realistic multi-step read.
        self._ambient(_READING_MATRIX, steps=self.ambient_steps)
        sections = self.mapper.resolve_all("profile_section")
        if sections:
            tgt = self.rng.choice(sections[: min(len(sections), 6)])
            self._move_to(tgt)
        return {"status": "ok", "sections": len(sections)}

    def _back(self) -> dict[str, Any]:
        back = self.mapper.resolve_one("back_control")
        if back is not None:
            self._move_to(back)
            try:
                back.click()
                self._settle()
                return {"status": "ok", "via": "back_control"}
            except Exception:
                logger.debug("[story] back_control click failed", exc_info=True)
        # No in-app back affordance is visible: we are already at the app root (e.g. a
        # prior tangent already restored the result list). We deliberately do NOT fall
        # back to the browser Back button — that can leave the app entirely (a fresh
        # tab's history is [about:blank, app], so Back lands on about:blank) and would
        # discard the live session. A truthful no-op is the correct, honest result.
        return {"status": "ok", "via": "noop"}

    def _settle(self) -> None:
        """Brief settle so the SPA swaps views before we re-resolve affordances."""
        try:
            self.page.wait_for_timeout(int(250 * max(self.dwell_scale, 0.2)))
        except Exception:
            pass
        # The view changed: drop cached affordance maps so re-resolution is fresh.
        self.mapper._cache.clear()

    # --- tangent handlers ---------------------------------------------------------
    def _do_tangent(self, scene: Scene) -> dict[str, Any]:
        name = scene.name
        if name == "tangent_open_profile":
            self._resume_stack.append({"resumes_after": scene.resumes_after, "kind": "profile"})
            self.tangents_fired += 1
            return self._open_profile(objective=False)
        if name == "tangent_read":
            return self._read_profile()
        if name == "tangent_research":
            self._resume_stack.append({"resumes_after": scene.resumes_after, "kind": "research"})
            self.tangents_fired += 1
            q = self.rng.choice(_OFFGOAL_QUERIES)
            return self._do_search_query(scene, query=q)
        if name == "tangent_refresh":
            self.tangents_fired += 1
            res = self._refresh_and_restore()
            self.tangents_returned += 1
            return res
        if name == "tangent_back":
            return self._tangent_return()
        return {"status": "unknown_tangent"}

    def _tangent_return(self) -> dict[str, Any]:
        ctx = self._resume_stack.pop() if self._resume_stack else {"kind": "profile"}
        self.tangents_returned += 1
        if ctx.get("kind") == "research":
            # Restore the goal's result set by re-running the objective query.
            return self._do_search_query_restore()
        return self._back()

    def _do_search_query_restore(self) -> dict[str, Any]:
        res = self._do_search_query(Scene("search_query", "main", 0), query=self.plan.query_hint)
        res["restored"] = True
        return res

    def _refresh_and_restore(self) -> dict[str, Any]:
        try:
            self.page.reload(wait_until="domcontentloaded")
        except Exception:
            try:
                self.page.go_back(wait_until="domcontentloaded")
            except Exception:
                logger.debug("[story] refresh failed", exc_info=True)
        self._settle()
        # After a refresh the SPA returns to its default view; re-run the objective
        # search so the goal result set is restored before resuming the main path.
        return self._do_search_query_restore()

    # --- main scene dispatch ------------------------------------------------------
    def _run_main(self, scene: Scene) -> dict[str, Any]:
        name = scene.name
        if name == "search_open":
            return self._do_search_open(scene)
        if name == "search_query":
            return self._do_search_query(scene)
        if name == "results_scan":
            return self._do_results_scan(scene)
        if name == "profile_open":
            res = self._open_profile(objective=True)
            if res.get("status") == "ok":
                self.state.profiles_opened += 1
            return res
        if name == "profile_read":
            self.state.profiles_read += 1
            return self._read_profile()
        if name == "profile_back":
            return self._back()
        return {"status": "unknown_main"}

    # --- run ----------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        update_status_ticker(self.page, "STORY MODE", f"goal={self.plan.goal_predicate.type}")
        for i, scene in enumerate(self.plan.scenes):
            try:
                if scene.tangent:
                    result = self._do_tangent(scene)
                else:
                    result = self._run_main(scene)
            except Exception as exc:  # noqa: BLE001 - record, never crash the walk
                result = {"status": "error", "error": repr(exc)}
                logger.debug("[story] scene %s raised", scene.name, exc_info=True)
            self._dwell(scene)
            entry = {
                "index": i,
                "scene": scene.name,
                "kind": scene.kind,
                "depth": scene.depth,
                **result,
            }
            if scene.tangent:
                entry["tangent"] = True
                entry["resumes_after"] = scene.resumes_after
                if scene.is_return:
                    entry["is_return"] = True
            self.trace.append(entry)
            if not scene.tangent and self.goal.is_satisfied(self.state):
                self._goal_met = True

        # The sampled plan is one human's chosen effort; if it didn't quite reach the
        # (jittered) goal, finish the task the way a person would rather than quitting
        # short. This is the "goal always reachable / always accomplished" guarantee.
        self._ensure_goal()
        return self.result_payload()

    def _ensure_goal(self) -> dict[str, Any]:
        """Top up objective progress until the goal predicate is satisfied."""
        ptype = self.plan.goal_predicate.type
        completion: dict[str, Any] = {"ran": False}
        if self.goal.is_satisfied(self.state):
            return completion
        completion["ran"] = True
        if ptype == "scan_fraction":
            res = self._do_results_scan(Scene("results_scan", "main", 0, target_role="result_list"))
            self.trace.append({"scene": "results_scan", "kind": "completion", **res})
        elif ptype in ("open_count", "find_in_profile"):
            guard = 0
            while not self.goal.is_satisfied(self.state) and guard < 12:
                guard += 1
                opened = self._open_profile(objective=True)
                if opened.get("status") == "ok":
                    self.state.profiles_opened += 1
                    if ptype == "find_in_profile":
                        self._read_profile()
                        self.state.profiles_read += 1
                    self.trace.append({"scene": "profile_open", "kind": "completion", **opened})
                    self._back()
                else:
                    break
        completion["goal_met"] = self.goal.is_satisfied(self.state)
        return completion

    def result_payload(self) -> dict[str, Any]:
        # Resume-stack balance is the audit that every wander returned.
        return {
            "goal": self.goal.to_payload(self.state),
            "goal_met": self.goal.is_satisfied(self.state),
            "tangents_fired": self.tangents_fired,
            "tangents_returned": self.tangents_returned,
            "profiles_opened_total": self.profiles_opened_total,
            "resume_stack_balanced": len(self._resume_stack) == 0,
            "scene_count": len(self.plan.scenes),
            "executed": len(self.trace),
            "trace": self.trace,
            "plan": self.plan.to_payload(),
        }


def run_story(
    page: Any,
    intent: TaskIntent,
    *,
    seed: Any = None,
    identity_id: str | None = None,
    recorder: SessionRecorder | None = None,
    dwell_scale: float = 1.0,
    ambient_steps: int = 5,
    max_scan_passes: int = 14,
    speed_scale: float = 1.0,
    behavior_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a StoryPlan from ``intent`` and execute it against ``page``.

    A behavior plan (identity kinematics + session seed) is built so the whole
    session runs under one identity with continuous analog motion. ``seed`` makes the
    session reproducible (same plan + same motor stream); omit it for genuinely
    different attempts.
    """
    if behavior_plan is None:
        target: dict[str, Any] = {"id": "story", "entity_type": "generic_web"}
        if identity_id:
            target["identity_id"] = identity_id
        behavior_plan = build_behavior_plan(target, provider="generic", goal="story_task_intent", seed=seed)
    behavior_plan.setdefault("recorder", {})["enabled"] = True
    behavior_plan["recorder"].setdefault("max_events", 500)

    plan = compile_story(intent, seed=seed)
    rec = recorder or SessionRecorder(behavior_plan)
    executor = StoryExecutor(
        page, plan, behavior_plan, recorder=rec,
        dwell_scale=dwell_scale, ambient_steps=ambient_steps,
        max_scan_passes=max_scan_passes, speed_scale=speed_scale,
    )
    result = executor.run()
    result["behavior_plan_run_id"] = behavior_plan.get("run_id")
    result["identity_id"] = behavior_plan.get("identity_id")
    result["recorder"] = rec.payload()
    return result
