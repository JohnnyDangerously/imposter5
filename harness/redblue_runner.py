"""Red-vs-Blue runner: drive the REAL imposter5 (Red) technology against a live
last-human-line (Blue) detection endpoint and capture both sides' telemetry.

No simulation: a real browser loads Blue's served /sandbox page, Red performs a
genuine human-like session using its actual interaction primitives (cubic-Bezier
moves, scroll-decay, cloak-humanized clicks/typing, and the stateful honeypot
EVASION engine), then submits Blue's telemetry form. We capture Blue's real
per-layer verdict (intercepted from the POST /api/lhhl/submit response) alongside
Red's own session-recorder trace and the honeypot-evasion outcome.

Engines:
  - cloak     : cloakbrowser launch (humanize patches Locator actions with wobble)
  - playwright: stock Playwright Chromium (no cloak humanize)
  - native    : the real installed Google Chrome, headed, on this laptop (authentic
                real-hardware client: real GPU/fonts/timezone, no SwiftShader)
  - auto      : try cloak, fall back to playwright

This module is import-light at top level; heavy imports happen inside main so
``--help`` works without a browser stack.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
SRC = REPO / "backend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))


def _start_mus_recording(page: Any) -> bool:
    """Inject the fp-agent mus.js recorder into the page and start capturing.

    Returns True if recording was armed. The recorder listens for real DOM mouse
    events, so every move Red makes (via page.mouse.move under the primitives) is
    captured in the exact frame format the FP-agent classifier consumes.
    """
    try:
        from harness.fp_agent_verdict import mus_js_source

        mus_src = mus_js_source()
        page.evaluate(
            "(function(){ if(!window.__fp_mus){ " + mus_src +
            " window.__fp_mus = new Mus(); window.__fp_mus.setTimePoint(true);} "
            "window.__fp_mus.record(); return true; })();"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] mus.js recording could not start: {exc!r}", file=sys.stderr)
        return False


def _arm_mus_persistent(page: Any) -> bool:
    """Arm the fp-agent mus.js recorder so it RE-INSTALLS on every document load.

    Story sessions may reload the page (a ``tangent_refresh`` curiosity excursion),
    which wipes any in-page recorder injected after load. Registering an init script
    makes each fresh document (initial load + every reload) auto-create and start the
    recorder, so the FP-agent still receives the post-navigation behavioral stream.
    Must be called BEFORE the initial ``page.goto`` so the first load is covered.
    """
    try:
        from harness.fp_agent_verdict import mus_js_source

        boot = (
            mus_js_source()
            + "\n(function(){ function __arm(){ try{ if(!window.__fp_mus){ "
            "window.__fp_mus = new Mus(); window.__fp_mus.setTimePoint(true);} "
            "window.__fp_mus.record(); }catch(e){} } "
            "if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded', __arm);} "
            "else { __arm(); } })();"
        )
        page.add_init_script(boot)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] persistent mus.js arming failed: {exc!r}", file=sys.stderr)
        return False


def _stop_mus_frames(page: Any) -> list:
    try:
        return page.evaluate(
            "(function(){ var m=window.__fp_mus; if(!m) return []; m.stop(); "
            "var d=m.getData(); return d.frames || []; })();"
        ) or []
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] mus.js frame capture failed: {exc!r}", file=sys.stderr)
        return []


def _build_plan(persona_name: str) -> dict[str, Any]:
    from imposter5.automation_connector.behavior_policy import build_behavior_plan, PERSONAS

    target = {"id": "redblue", "entity_id": "lhhl-sandbox", "entity_type": "generic_web"}
    plan = build_behavior_plan(target, provider="generic", goal="observe_visible_page_state")

    for p in PERSONAS:
        if p.name == persona_name:
            plan["persona"] = {
                "name": p.name,
                "patience": p.patience,
                "scroll_style": p.scroll_style,
                "interaction_style": p.interaction_style,
            }
            break

    plan.setdefault("recorder", {})["enabled"] = True
    plan["recorder"]["max_events"] = 500
    return plan


def _launch(engine: str, headless: bool):
    """Return (closer_callable, page, engine_used).

    closer_callable() tears the browser/context/playwright down cleanly.
    """
    from imposter5.loaders.cloak_runtime import (
        apply_anti_fingerprint_init_script,
        automation_connector_stealth_context_kwargs,
    )

    ctx_kwargs = automation_connector_stealth_context_kwargs()
    # Harness targets our own Blue, which serves a self-signed cert in the AWS
    # deployment. Accept it so the real TLS handshake (real JA3) still happens
    # without a trusted-CA requirement. This does not change Red's fingerprint.
    ctx_kwargs["ignore_https_errors"] = True

    # Optional watchable recording: set HARNESS_VIDEO_DIR to capture a .webm of the
    # session (e.g. to eyeball whether the motion looks natural). No effect when unset.
    _video_dir = os.environ.get("HARNESS_VIDEO_DIR")
    if _video_dir:
        ctx_kwargs["record_video_dir"] = _video_dir
        ctx_kwargs["record_video_size"] = {"width": 1280, "height": 800}

    if engine in ("auto", "cloak"):
        try:
            from imposter5.loaders.cloak_runtime import launch_automation_browser

            browser = launch_automation_browser(headless=headless)
            context = browser.new_context(**ctx_kwargs)
            apply_anti_fingerprint_init_script(context)
            page = context.new_page()

            def _close() -> None:
                try:
                    context.close()
                finally:
                    browser.close()

            return _close, page, "cloak"
        except Exception as exc:  # noqa: BLE001 - report and fall back honestly
            if engine == "cloak":
                raise
            print(f"[runner] cloak engine unavailable ({exc!r}); falling back to playwright", file=sys.stderr)

    from playwright.sync_api import sync_playwright

    # Native engine: the REAL installed Google Chrome, headed, on this laptop.
    # This is the authentic real-hardware client — real Apple GPU (no SwiftShader),
    # real OS fonts/timezone, real channel build — so Blue's Layer-2 environment
    # signals reflect a genuine machine rather than a headless server sandbox.
    if engine == "native":
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=False, channel="chrome")
            engine_used = "native-chrome"
        except Exception as exc:  # noqa: BLE001 - Chrome channel not installed
            print(f"[runner] real Chrome unavailable ({exc!r}); using headed Chromium", file=sys.stderr)
            browser = pw.chromium.launch(headless=False)
            engine_used = "native-chromium"
        context = browser.new_context(**ctx_kwargs)
        apply_anti_fingerprint_init_script(context)
        page = context.new_page()

        def _close_native() -> None:
            try:
                context.close()
            finally:
                browser.close()
                pw.stop()

        return _close_native, page, engine_used

    # Playwright fallback (no cloak humanize patch).
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context(**ctx_kwargs)
    apply_anti_fingerprint_init_script(context)
    page = context.new_page()

    def _close() -> None:
        try:
            context.close()
        finally:
            browser.close()
            pw.stop()

    return _close, page, "playwright"


def _drive_session(page: Any, plan: dict[str, Any], recorder: Any) -> dict[str, Any]:
    """Perform a controlled-but-REAL human-like session with Red's primitives.

    Avoids randomly clicking the page's control buttons (so the form is not
    submitted prematurely) while still exercising the genuine code paths and,
    crucially, the honeypot-evasion engine against Blue's ADMIN-BYPASS trap.
    """
    from imposter5.automation_connector.interaction_primitives import (
        click_element,
        hover_element,
        move_pointer,
        scroll_page,
        type_text,
    )

    notes: dict[str, Any] = {}

    # 1. A few human-like cursor moves over the sandbox region (real cubic Bezier).
    for (mx, my) in ((360, 300), (820, 420), (560, 520), (980, 360)):
        move_pointer(page, mx, my, plan, recorder=recorder)
        page.wait_for_timeout(180)

    # 2. Real scroll-decay passes.
    scroll_page(page, plan, pass_index=0, fallback_delta_y=520, recorder=recorder)
    page.wait_for_timeout(300)
    scroll_page(page, plan, pass_index=1, fallback_delta_y=380, recorder=recorder)
    page.wait_for_timeout(250)

    # 3. Hover a benign element.
    try:
        hover_element(page, "#text-input", plan, recorder=recorder)
    except Exception as exc:  # noqa: BLE001
        notes["hover_error"] = repr(exc)

    # 4. Type into the real text field (cloak-humanized when engine=cloak).
    try:
        type_text(page, "#text-input", "the last human line", plan, recorder=recorder)
    except Exception as exc:  # noqa: BLE001
        notes["type_error"] = repr(exc)

    # 5. Honeypot-evasion probe: ask Red to click Blue's hidden ADMIN BYPASS trap.
    #    Red's stateful honeypot engine should DETECT (tabindex=-1 / hidden) and SKIP.
    try:
        hp_result = click_element(page, "#honeypot-btn", plan, recorder=recorder)
        notes["honeypot_probe"] = hp_result
        notes["honeypot_evaded"] = bool(hp_result.get("honeypot_evaded"))
    except Exception as exc:  # noqa: BLE001
        notes["honeypot_probe_error"] = repr(exc)
        notes["honeypot_evaded"] = None

    # 6. One more move so post-typing motion is present.
    move_pointer(page, 640, 300, plan, recorder=recorder)
    page.wait_for_timeout(200)

    return notes


def _drive_markov(page: Any, plan: dict[str, Any], recorder: Any) -> dict[str, Any]:
    """Run Red's flagship semi-Markov pathing simulator unconstrained.

    Used against an auto-submit target (``?auto=N``) so the simulator's random
    clicks (including Blue's honeypot, which Red's evasion engine should skip)
    cannot trigger an early submit.
    """
    from imposter5.loaders.markov_simulator import run_markov_simulation

    notes: dict[str, Any] = {}
    try:
        sim = run_markov_simulation(page, plan, recorder=recorder, max_steps=12)
        notes["steps_executed"] = sim.get("steps_executed")
        notes["state_history"] = sim.get("state_history")
        notes["final_state"] = sim.get("final_state")
    except Exception as exc:  # noqa: BLE001
        notes["markov_error"] = repr(exc)
    return notes


def _drive_story(
    page: Any,
    plan: dict[str, Any],
    recorder: Any,
    *,
    task_intent: str | None,
    seed: Any = None,
    dwell_scale: float = 1.0,
    ambient_steps: int = 5,
    max_scan_passes: int = 14,
    speed_scale: float = 1.0,
) -> dict[str, Any]:
    """Run Story Mode: a TaskIntent-driven, goal-oriented, human-RANDOM session.

    Loads + validates the TaskIntent, then drives Red's real analog motor + semi-Markov
    micro-behavior through ``run_story`` against the already-navigated target page. The
    page's real ``page.mouse.move`` calls feed the mus.js recorder exactly as the other
    modes do, so the FP-agent classifier sees a genuine behavioral stream.
    """
    from imposter5.story.executor import run_story
    from imposter5.story.task_intent import load_task_intent

    default_intent = str((HERE / "fixtures" / "task_intent_data_engineers.json"))
    intent = load_task_intent(task_intent or default_intent)

    story = run_story(
        page, intent,
        seed=seed,
        recorder=recorder,
        behavior_plan=plan,
        dwell_scale=dwell_scale,
        ambient_steps=ambient_steps,
        max_scan_passes=max_scan_passes,
        speed_scale=speed_scale,
    )
    # Honeypots untouched is an audit derived from the executed trace: no opened
    # element key carried a trap signature.
    opened_keys = [e.get("element", "") for e in story.get("trace", []) if "element" in e]
    honeypot_evaded = not any("trap" in k for k in opened_keys)
    return {
        "intent": {
            "site": intent.site,
            "archetype": intent.archetype,
            "describe": intent.describe,
            "query_hint": intent.query_hint,
            "goal_predicate": {
                "type": intent.goal_predicate.type,
                "target": intent.goal_predicate.target,
                "jitter": intent.goal_predicate.jitter,
            },
        },
        "goal_met": story.get("goal_met"),
        "goal": story.get("goal"),
        "tangents_fired": story.get("tangents_fired"),
        "tangents_returned": story.get("tangents_returned"),
        "resume_stack_balanced": story.get("resume_stack_balanced"),
        "profiles_opened_total": story.get("profiles_opened_total"),
        "scene_count": story.get("scene_count"),
        "executed": story.get("executed"),
        "honeypot_evaded": honeypot_evaded,
        "trace": story.get("trace"),
    }


def run_matchup(
    *,
    target_url: str,
    persona: str,
    engine: str,
    headless: bool,
    mode: str = "controlled",
    auto_seconds: int = 90,
    task_intent: str | None = None,
    seed: Any = None,
    dwell_scale: float = 1.0,
    ambient_steps: int = 5,
    max_scan_passes: int = 14,
    speed_scale: float = 1.0,
) -> dict[str, Any]:
    from imposter5.automation_connector.session_recorder import SessionRecorder

    plan = _build_plan(persona)
    if mode == "markov":
        plan["use_markov_pathing"] = True
    recorder = SessionRecorder(plan)

    closer, page, engine_used = _launch(engine, headless)
    captured: dict[str, Any] = {}

    def _on_response(resp: Any) -> None:
        try:
            if resp.url.rstrip("/").endswith("/api/lhhl/submit"):
                captured["status"] = resp.status
                captured["body"] = resp.json()
        except Exception as exc:  # noqa: BLE001
            captured["capture_error"] = repr(exc)

    # In markov mode the target auto-submits on a timer (backstop) while Red
    # browses freely; we still trigger submit explicitly once the sim finishes.
    nav_url = target_url
    if mode == "markov" and "auto=" not in nav_url:
        sep = "&" if "?" in nav_url else "?"
        nav_url = f"{nav_url}{sep}auto={auto_seconds}"

    started = time.monotonic()
    mus_frames: list = []
    mus_armed = False
    try:
        page.on("response", _on_response)
        # Story sessions can reload mid-run, so arm a PERSISTENT recorder (init script)
        # before the first load; other modes inject after load as before.
        if mode == "story":
            mus_armed = _arm_mus_persistent(page)
        page.goto(nav_url, wait_until="domcontentloaded")
        page.wait_for_timeout(400)

        # When recording, draw the synthetic cursor overlay so the .webm shows the
        # analog pointer path (init-script based, so it survives story reloads).
        if os.environ.get("HARNESS_VIDEO_DIR"):
            try:
                from imposter5.automation_connector.interaction_primitives import inject_synthetic_cursor

                inject_synthetic_cursor(page)
            except Exception:  # noqa: BLE001 - recording overlay is best-effort
                pass

        # Arm the real fp-agent mus.js recorder before driving so the partner's
        # XGBoost sees the same behavioral stream Blue's statistical L3 does.
        if mode != "story":
            mus_armed = _start_mus_recording(page)
        page.wait_for_timeout(150)

        if mode == "story":
            session_notes = _drive_story(
                page, plan, recorder,
                task_intent=task_intent, seed=seed,
                dwell_scale=dwell_scale, ambient_steps=ambient_steps,
                max_scan_passes=max_scan_passes, speed_scale=speed_scale,
            )
            # Best-effort submit: if the target IS Blue's sandbox it exposes
            # submitTelemetry() and we capture the verdict; on a generic site (or our
            # local fixture) this is a harmless no-op and Blue's report stays empty.
            try:
                with page.expect_response(lambda r: r.url.rstrip("/").endswith("/api/lhhl/submit"), timeout=8000):
                    page.evaluate("window.submitTelemetry && window.submitTelemetry()")
            except Exception:  # noqa: BLE001 - generic targets have no submit endpoint
                pass
        elif mode == "markov":
            session_notes = _drive_markov(page, plan, recorder)
            # Trigger submit explicitly now that the session is done; the page's
            # auto timer is a backstop in case this evaluate is too early/late.
            try:
                with page.expect_response(lambda r: r.url.rstrip("/").endswith("/api/lhhl/submit"), timeout=20000):
                    page.evaluate("window.submitTelemetry && window.submitTelemetry()")
            except Exception as exc:  # noqa: BLE001
                session_notes["submit_trigger_error"] = repr(exc)
        else:
            session_notes = _drive_session(page, plan, recorder)
            # Explicit, deterministic submit of Blue's telemetry form, then await the verdict.
            with page.expect_response(lambda r: r.url.rstrip("/").endswith("/api/lhhl/submit"), timeout=15000):
                page.locator("#submit-btn").click()
        page.wait_for_timeout(300)

        # Pull the captured behavioral frames for the FP-agent classifier.
        if mus_armed:
            mus_frames = _stop_mus_frames(page)
    finally:
        duration_s = round(time.monotonic() - started, 2)
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass

    # Run the partner's REAL fp-agent XGBoost on the captured mus frames.
    try:
        from harness.fp_agent_verdict import fp_agent_verdict

        fp_result = fp_agent_verdict(mus_frames)
    except Exception as exc:  # noqa: BLE001
        fp_result = {"status": "error", "note": f"{type(exc).__name__}: {str(exc)[:200]}"}
    fp_result.setdefault("mus_frames_captured", len(mus_frames))

    blue_report = (captured.get("body") or {}).get("data") or captured.get("body") or {}

    red_payload = recorder.payload()
    action_breakdown: dict[str, int] = {}
    for ev in red_payload.get("events", []):
        action_breakdown[ev.get("action", "?")] = action_breakdown.get(ev.get("action", "?"), 0) + 1

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "engine_used": engine_used,
        "persona": persona,
        "mode": mode,
        "target_url": nav_url,
        "duration_s": duration_s,
        "red_self": {
            "event_count": red_payload.get("event_count"),
            "action_breakdown": action_breakdown,
            "honeypot_evaded": session_notes.get("honeypot_evaded"),
            "session_notes": session_notes,
        },
        "blue_report": {
            "evasion_score": blue_report.get("evasion_score"),
            "verdict": blue_report.get("verdict"),
            "critical_leak_count": blue_report.get("critical_leak_count"),
            "layers": blue_report.get("layers"),
            "kinetics_present": bool(blue_report.get("kinetics")),
        },
        "fp_agent": fp_result,
        "blue_capture_meta": {k: v for k, v in captured.items() if k != "body"},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a real Red(imposter5)-vs-Blue(last-human-line) matchup.")
    parser.add_argument("--target-url", default=os.environ.get("BLUE_BASE_URL", "http://127.0.0.1:5190").rstrip("/") + "/sandbox")
    parser.add_argument("--persona", default=os.environ.get("PERSONA", "focused_power_user"))
    parser.add_argument("--engine", default=os.environ.get("ENGINE", "auto"), choices=["auto", "cloak", "playwright", "native"])
    parser.add_argument("--mode", default=os.environ.get("MODE", "controlled"), choices=["controlled", "markov", "story"],
                        help="controlled: scripted real-primitive session + explicit submit; "
                             "markov: Red's full semi-Markov simulator vs an auto-submit target; "
                             "story: a TaskIntent-driven, goal-oriented, human-RANDOM session")
    parser.add_argument("--auto-seconds", type=int, default=int(os.environ.get("AUTO_SECONDS", "90")))
    parser.add_argument("--task-intent", default=os.environ.get("TASK_INTENT"),
                        help="story mode: path to a TaskIntent JSON file OR inline JSON "
                             "(default: harness/fixtures/task_intent_data_engineers.json)")
    parser.add_argument("--seed", default=os.environ.get("STORY_SEED"),
                        help="story mode: session seed for a reproducible plan + motor stream")
    parser.add_argument("--dwell-scale", type=float, default=float(os.environ.get("DWELL_SCALE", "1.0")),
                        help="story mode: scale scene dwell times (1.0 = full realistic timing)")
    parser.add_argument("--ambient-steps", type=int, default=int(os.environ.get("AMBIENT_STEPS", "5")),
                        help="story mode: semi-Markov ambient micro-behavior steps per read/scan")
    parser.add_argument("--max-scan-passes", type=int, default=int(os.environ.get("MAX_SCAN_PASSES", "14")),
                        help="story mode: cap on results-scan wheel passes")
    parser.add_argument("--speed-scale", type=float, default=float(os.environ.get("SPEED_SCALE", "1.0")),
                        help="story mode: compress motor per-step sleeps (1.0 = full realism; "
                             "use a small value like 0.05 for a fast dry run)")
    parser.add_argument("--out", default=str(HERE / "out" / "red.json"))
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    args = parser.parse_args(argv)

    result = run_matchup(
        target_url=args.target_url,
        persona=args.persona,
        engine=args.engine,
        headless=args.headless,
        mode=args.mode,
        auto_seconds=args.auto_seconds,
        task_intent=args.task_intent,
        seed=args.seed,
        dwell_scale=args.dwell_scale,
        ambient_steps=args.ambient_steps,
        max_scan_passes=args.max_scan_passes,
        speed_scale=args.speed_scale,
    )

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    b = result["blue_report"]
    r = result["red_self"]
    fp = result["fp_agent"]
    print(f"[runner] engine={result['engine_used']} persona={result['persona']} mode={result['mode']} -> "
          f"Blue(stats): {b.get('evasion_score')}% / {b.get('verdict')} "
          f"(critical leaks {b.get('critical_leak_count')}); "
          f"Red: {r.get('event_count')} events, honeypot_evaded={r.get('honeypot_evaded')}")
    if result["mode"] == "story":
        sn = r.get("session_notes", {})
        goal = sn.get("goal", {}) or {}
        st = goal.get("state", {}) or {}
        print(f"[runner] story: goal_met={sn.get('goal_met')} "
              f"({goal.get('type')} eff_target={goal.get('effective_target')}, "
              f"scan_fraction={st.get('scan_fraction')}); "
              f"tangents {sn.get('tangents_fired')} fired / {sn.get('tangents_returned')} returned "
              f"(balanced={sn.get('resume_stack_balanced')}); "
              f"profiles_opened_total={sn.get('profiles_opened_total')}")
    if fp.get("status") == "ok":
        print(f"[runner] FP-agent(XGBoost): {fp.get('verdict')} as '{fp.get('predicted_label')}' "
              f"(conf {fp.get('confidence')}, P(Human)={fp.get('human_probability')}, "
              f"{fp.get('n_mouse_frames')} mouse frames)")
    else:
        print(f"[runner] FP-agent: {fp.get('status')} — {fp.get('note', '')}")
    print(f"[runner] report written to {out_path}")
    if result["mode"] == "story":
        # On a generic target there is no Blue verdict; success is goal completion.
        return 0 if r.get("session_notes", {}).get("goal_met") else 2
    return 0 if b.get("verdict") else 2


if __name__ == "__main__":
    raise SystemExit(main())
