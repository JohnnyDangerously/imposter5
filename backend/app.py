from __future__ import annotations

import os
import sys
import json
import shutil
import tempfile
import logging
from pathlib import Path
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from pydantic import BaseModel, Field

# Ensure backend root and src directory are in sys.path
backend_dir = str(Path(__file__).parent.resolve())
src_dir = str(Path(backend_dir) / "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from imposter5.errors import success_response, error_response
from imposter5.automation_connector.models import Imposter5RunRequest

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("imposter5")

app = FastAPI(title="Imposter5 API", version="1.0.0")

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_fp_agent_verdict(frames: list) -> dict | None:
    try:
        from imposter5.fp_agent.fp_agent_local_redteam_detector_test import try_real_fp_agent_verdict
        return try_real_fp_agent_verdict(frames, print_header=False)
    except Exception as e:
        logger.error(f"[imposter5] could not import try_real_fp_agent_verdict: {e}")
        return None


class WebsiteSaveRequest(BaseModel):
    name: str
    url: str
    description: str = ""


class PersonaSaveRequest(BaseModel):
    name: str
    patience: str
    scroll_style: str
    dwell_multiplier: float
    scroll_multiplier: float
    interaction_style: str = "low_touch"


class LoginStartRequest(BaseModel):
    user_id: str
    url: str
    mode: str = "interactive"


class LoginVerifyRequest(BaseModel):
    user_id: str
    url: str


class CookieSetRequest(BaseModel):
    user_id: str
    url: str
    cookies: list[dict[str, Any]]


WEBSITES_FILE_PATH = os.path.join(os.path.dirname(__file__), "src", "imposter5", "automation_connector", "websites.json")

DEFAULT_WEBSITES = [
    {"name": "LinkedIn Feed", "url": "https://www.linkedin.com/feed", "description": "Persistent LinkedIn feed simulation. Supports advanced feed reading, comment expansion, profile peeks, notifications checks, and avatar clicks."},
    {"name": "Wikipedia AI", "url": "https://en.wikipedia.org/wiki/Artificial_intelligence", "description": "Public Wikipedia article on AI. Great for testing multi-page link clicks and reading pauses."},
    {"name": "Wikipedia Machine Learning", "url": "https://en.wikipedia.org/wiki/Machine_learning", "description": "Wikipedia article on ML. Perfect for testing search-like queries and scroll-and-read behaviors."},
    {"name": "Yahoo News", "url": "https://news.yahoo.com", "description": "Dynamic news portal. Good for testing feed scans, comments, and fast-paced browsing."},
    {"name": "Hacker News", "url": "https://news.ycombinator.com", "description": "Text-heavy tech feed. Ideal for testing precise, low-touch link clicks and methodic scanner pacing."}
]


def load_websites() -> list[dict[str, Any]]:
    try:
        if os.path.exists(WEBSITES_FILE_PATH):
            with open(WEBSITES_FILE_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass

    try:
        os.makedirs(os.path.dirname(WEBSITES_FILE_PATH), exist_ok=True)
        with open(WEBSITES_FILE_PATH, "w") as f:
            json.dump(DEFAULT_WEBSITES, f, indent=2)
    except Exception:
        pass
    return DEFAULT_WEBSITES


def _save_websites_to_disk(websites: list[dict[str, Any]]) -> None:
    try:
        os.makedirs(os.path.dirname(WEBSITES_FILE_PATH), exist_ok=True)
        with open(WEBSITES_FILE_PATH, "w") as f:
            json.dump(websites, f, indent=2)
    except Exception:
        pass


@app.get("/api/imposter5/personas")
async def imposter5_personas():
    """Return available personas for the UI."""
    try:
        from imposter5.automation_connector.behavior_policy import PERSONAS
        return success_response({
            "personas": [
                {
                    "name": p.name,
                    "patience": p.patience,
                    "scroll_style": p.scroll_style,
                    "dwell_multiplier": p.dwell_multiplier,
                    "scroll_multiplier": p.scroll_multiplier,
                    "interaction_style": p.interaction_style,
                }
                for p in PERSONAS
            ]
        })
    except Exception as e:
        return error_response("personas_error", str(e))


@app.post("/api/imposter5/personas")
async def save_persona(body: PersonaSaveRequest):
    """Save or update a custom behavior pack (persona)."""
    try:
        from imposter5.automation_connector.behavior_policy import PERSONAS_FILE_PATH, save_personas
        
        # Load existing personas raw
        existing = []
        if os.path.exists(PERSONAS_FILE_PATH):
            try:
                with open(PERSONAS_FILE_PATH, "r") as f:
                    existing = json.load(f)
            except Exception:
                pass
        
        # Remove if name already exists to overwrite
        existing = [p for p in existing if p["name"] != body.name]
        existing.append(body.model_dump())
        
        save_personas(existing)
        return success_response({"success": True, "message": f"Persona '{body.name}' saved successfully."})
    except Exception as e:
        return error_response("save_persona_error", str(e))


@app.delete("/api/imposter5/personas/{name}")
async def delete_persona(name: str):
    """Delete a custom behavior pack (persona)."""
    try:
        from imposter5.automation_connector.behavior_policy import PERSONAS_FILE_PATH, save_personas
        
        existing = []
        if os.path.exists(PERSONAS_FILE_PATH):
            try:
                with open(PERSONAS_FILE_PATH, "r") as f:
                    existing = json.load(f)
            except Exception:
                pass
                
        filtered = [p for p in existing if p["name"] != name]
        save_personas(filtered)
        return success_response({"success": True, "message": f"Persona '{name}' deleted successfully."})
    except Exception as e:
        return error_response("delete_persona_error", str(e))


@app.get("/api/imposter5/websites")
async def get_websites():
    """Return saved target websites."""
    try:
        websites = load_websites()
        return success_response({"websites": websites})
    except Exception as e:
        return error_response("websites_error", str(e))


@app.post("/api/imposter5/websites")
async def save_website(body: WebsiteSaveRequest):
    """Save or update a target website."""
    try:
        websites = load_websites()
        # Overwrite if name already exists
        websites = [w for w in websites if w["name"] != body.name]
        websites.append(body.model_dump())
        _save_websites_to_disk(websites)
        return success_response({"success": True, "message": f"Website '{body.name}' saved successfully."})
    except Exception as e:
        return error_response("save_website_error", str(e))


@app.delete("/api/imposter5/websites/{name}")
async def delete_website(name: str):
    """Delete a saved target website."""
    try:
        websites = load_websites()
        filtered = [w for w in websites if w["name"] != name]
        _save_websites_to_disk(filtered)
        return success_response({"success": True, "message": f"Website '{name}' deleted successfully."})
    except Exception as e:
        return error_response("delete_website_error", str(e))


def sanitize_human_config(config: dict[str, Any]) -> dict[str, Any]:
    """Ensure human_config keys and range formats are fully compatible with cloakbrowser HumanConfig."""
    sanitized = {}
    for k, v in config.items():
        if k in ("mouse_overshoot_px", "mouse_burst_size", "mouse_burst_pause", "click_aim_delay_button", "click_aim_delay_input", "click_hold_input", "click_hold_button", "idle_pause_range", "scroll_delta_base", "scroll_pause_fast", "scroll_pause_slow", "scroll_accel_steps", "scroll_decel_steps", "scroll_overshoot_px", "scroll_settle_delay", "scroll_target_zone", "scroll_pre_move_delay", "initial_cursor_x", "initial_cursor_y", "idle_between_duration", "shift_down_delay", "shift_up_delay", "key_hold", "mistype_delay_notice", "mistype_delay_correct", "field_switch_delay", "typing_pause_range"):
            if isinstance(v, (list, tuple)) and len(v) == 2:
                sanitized[k] = v
            continue
        if k.endswith("_min") or k.endswith("_max"):
            continue
        sanitized[k] = v

    # Reconstruct ranges from min/max keys if they exist
    for prefix in ("mouse_overshoot_px", "mouse_burst_size", "mouse_burst_pause", "click_aim_delay_button", "click_aim_delay_input", "click_hold_input", "click_hold_button", "idle_pause_range", "scroll_delta_base", "scroll_pause_fast", "scroll_pause_slow", "scroll_accel_steps", "scroll_decel_steps", "scroll_overshoot_px", "scroll_settle_delay", "scroll_target_zone", "scroll_pre_move_delay", "initial_cursor_x", "initial_cursor_y", "idle_between_duration", "shift_down_delay", "shift_up_delay", "key_hold", "mistype_delay_notice", "mistype_delay_correct", "field_switch_delay", "typing_pause_range"):
        min_val = config.get(f"{prefix}_min")
        max_val = config.get(f"{prefix}_max")
        if min_val is not None and max_val is not None:
            sanitized[prefix] = [min_val, max_val]

    return sanitized


@app.post("/api/imposter5/run")
def imposter5_run(body: Imposter5RunRequest):
    """Launch an Imposter5 red team simulation with custom behavior packs and techniques."""
    # Build the behavior plan
    from imposter5.automation_connector.behavior_policy import build_behavior_plan, PERSONAS
    from imposter5.automation_connector.goals import goal_spec_from_payload
    from imposter5.automation_connector.goal_runner import run_visible_state_goal
    from imposter5.automation_connector.session_recorder import SessionRecorder
    from imposter5.automation_connector.interaction_primitives import enable_visible_mouse_tracking
    from imposter5.automation_connector.browser_runner import get_browser_runner

    # 1. Build base plan
    fake_target = {
        "id": "imposter5-sim",
        "entity_id": body.url,
        "entity_type": "generic_web" if body.provider != "linkedin" else "linkedin_profile",
    }
    
    plan = build_behavior_plan(
        fake_target,
        provider=body.provider,
        goal="observe_visible_page_state",
    )

    # 2. Apply custom persona override
    if body.persona:
        matched = None
        for p in PERSONAS:
            if p.name == body.persona:
                matched = p
                break
        if matched:
            plan["persona"] = {
                "name": matched.name,
                "patience": matched.patience,
                "scroll_style": matched.scroll_style,
                "interaction_style": matched.interaction_style,
            }

    # 3. Apply custom completion override
    if body.completion:
        plan["completion"] = body.completion

    # Always enable the session recorder for Imposter5 runs to drive interactive timeline
    plan.setdefault("recorder", {})["enabled"] = True
    plan["recorder"]["max_events"] = 500

    # 4. Apply custom variations
    if body.variations:
        plan.setdefault("variations", {}).update(body.variations)

    # 5. Apply custom human_config
    if body.human_config:
        sanitized_cfg = sanitize_human_config(body.human_config)
        os.environ["AUTOMATION_CONNECTOR_HUMAN_CONFIG"] = json.dumps(sanitized_cfg)
    else:
        os.environ.pop("AUTOMATION_CONNECTOR_HUMAN_CONFIG", None)

    # 6. Set up video directory
    video_dir = tempfile.mkdtemp(prefix="tokyo-imposter5-movie-")
    
    import threading

    # Run the entire simulation in a separate thread to completely bypass
    # Playwright's "using Playwright Sync API inside the asyncio loop" check.
    # A standard threading.Thread starts a clean OS thread with no asyncio loop.
    
    logs = []
    logs.append(f"[{datetime.now().isoformat()}] Starting Imposter5 simulation on {body.url}")
    logs.append(f"[{datetime.now().isoformat()}] Provider: {body.provider}, Persona: {plan.get('persona', {}).get('name')}")
    logs.append(f"[{datetime.now().isoformat()}] Completion: {plan.get('completion')}")
    logs.append(f"[{datetime.now().isoformat()}] Variations: {json.dumps(plan.get('variations'))}")

    all_captured_frames = []
    movie_filename = ""
    stamped_codex_path = ""
    latest_codex_path = ""
    goal_payload = None
    session_recorder_instance = None
    posts = []

    def run_simulation_thread():
        nonlocal all_captured_frames, movie_filename, stamped_codex_path, latest_codex_path, goal_payload, session_recorder_instance, posts
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            logs.append(f"[{datetime.now().isoformat()}] Thread running loop detected: {loop}")
        except RuntimeError:
            logs.append(f"[{datetime.now().isoformat()}] Thread running loop: None")
        try:
            loop = asyncio.get_event_loop()
            logs.append(f"[{datetime.now().isoformat()}] Thread event loop detected: {loop}")
        except RuntimeError as e:
            logs.append(f"[{datetime.now().isoformat()}] Thread event loop error: {e}")
        try:
            # Run the actual goal
            if body.provider == "linkedin":
                logs.append(f"[{datetime.now().isoformat()}] Executing LinkedIn feed scrape")
                from imposter5.loaders.linkedin_feed_scraper import scrape_feed
                try:
                    posts = scrape_feed(
                        "visible_watch_session",
                        raise_on_error=True,
                        behavior_plan=plan,
                        headless=False,
                        visible=True,
                        record_video_dir=video_dir,
                    )
                    logs.append(f"[{datetime.now().isoformat()}] LinkedIn feed scrape completed, found {len(posts)} posts")
                except Exception as exc:
                    logs.append(f"[{datetime.now().isoformat()}] LinkedIn scrape raised: {exc}")
            elif "wikipedia.org" in body.url.lower():
                logs.append(f"[{datetime.now().isoformat()}] Executing advanced Wikipedia simulation")
                runner = get_browser_runner()
                browser = runner.launch_browser(headless=False)

                from imposter5.loaders.cloak_runtime import (
                    apply_anti_fingerprint_init_script,
                    automation_connector_stealth_context_kwargs,
                )

                ctx_kwargs = automation_connector_stealth_context_kwargs()
                ctx_kwargs["record_video_dir"] = video_dir
                ctx_kwargs["record_video_size"] = {"width": 1440, "height": 900}
                
                context = browser.new_context(**ctx_kwargs)
                if plan.get("persona", {}).get("name") != "naive_bot":
                    try:
                        apply_anti_fingerprint_init_script(context)
                    except Exception:
                        pass
                context.set_default_timeout(25_000)
                page = context.new_page()

                # Enable console logging for debugging
                page.on("console", lambda msg: print(f"[BROWSER CONSOLE] {msg.text}", flush=True))
                page.on("pageerror", lambda exc: print(f"[BROWSER EXCEPTION] {exc}", flush=True))

                # Enable visible mouse tracking
                enable_visible_mouse_tracking(page)
                logs.append(f"[{datetime.now().isoformat()}] Injected red synthetic HUMAN MOUSE cursor overlay")

                try:
                    page.bring_to_front()
                except Exception:
                    pass

                logs.append(f"[{datetime.now().isoformat()}] Navigating to target Wikipedia URL: {body.url}")
                page.goto(body.url, wait_until="domcontentloaded")
                page.wait_for_timeout(1000)

                recorder = SessionRecorder(plan)
                session_recorder_instance = recorder
                from imposter5.loaders.wikipedia_simulator import run_wikipedia_simulation
                run_wikipedia_simulation(page, plan, recorder=recorder)
                logs.append(f"[{datetime.now().isoformat()}] Wikipedia simulation completed.")

                # Close context and browser to finalize movie
                context.close()
                browser.close()
                logs.append(f"[{datetime.now().isoformat()}] Finalized video recording")
            else:
                # 7. Launch browser for generic web simulation
                runner = get_browser_runner()
                browser = runner.launch_browser(headless=False)

                from imposter5.loaders.cloak_runtime import (
                    apply_anti_fingerprint_init_script,
                    automation_connector_stealth_context_kwargs,
                )

                ctx_kwargs = automation_connector_stealth_context_kwargs()
                ctx_kwargs["record_video_dir"] = video_dir
                ctx_kwargs["record_video_size"] = {"width": 1440, "height": 900}
                
                context = browser.new_context(**ctx_kwargs)
                if plan.get("persona", {}).get("name") != "naive_bot":
                    try:
                        apply_anti_fingerprint_init_script(context)
                    except Exception:
                        pass
                context.set_default_timeout(25_000)
                page = context.new_page()

                # Enable console logging for debugging
                page.on("console", lambda msg: print(f"[BROWSER CONSOLE] {msg.text}", flush=True))
                page.on("pageerror", lambda exc: print(f"[BROWSER EXCEPTION] {exc}", flush=True))

                # Enable visible mouse tracking
                enable_visible_mouse_tracking(page)
                logs.append(f"[{datetime.now().isoformat()}] Injected red synthetic HUMAN MOUSE cursor overlay")

                try:
                    page.bring_to_front()
                except Exception:
                    pass

                # If running fp-agent, start mus recording
                if body.run_fp_agent:
                    logs.append(f"[{datetime.now().isoformat()}] Starting mus.js behavioral recording for fp-agent analysis")
                    try:
                        from imposter5.fp_agent.fp_agent_local_redteam_detector_test import ensure_mus_recording
                        ensure_mus_recording(page)
                    except Exception as e:
                        logs.append(f"[{datetime.now().isoformat()}] Warning: could not start mus recording: {e}")

                logs.append(f"[{datetime.now().isoformat()}] Navigating to target URL: {body.url}")
                page.goto(body.url, wait_until="domcontentloaded")
                page.wait_for_timeout(1000)

                recorder = SessionRecorder(plan)
                session_recorder_instance = recorder
                if body.prompt:
                    from imposter5.automation_connector.goals import goal_spec_from_natural_prompt
                    logs.append(f"[{datetime.now().isoformat()}] Interpreting natural-language prompt: {body.prompt}")
                    goal = goal_spec_from_natural_prompt(body.prompt, start_url=body.url)
                    logs.append(f"[{datetime.now().isoformat()}] Compiled prompt into goal: {goal.name} with {len(goal.steps)} steps")
                else:
                    goal = goal_spec_from_payload(
                        {"goal": "observe_visible_page_state", "completion": "skim_visible_feed"},
                        fallback_start_url=body.url,
                    )
                    logs.append(f"[{datetime.now().isoformat()}] Running generic skim goal actions")

                if plan.get("use_markov_pathing") or (body.prompt and "markov" in body.prompt.lower()):
                    logs.append(f"[{datetime.now().isoformat()}] Running dynamic Markov Chain Pathing Simulator")
                    from imposter5.loaders.markov_simulator import run_markov_simulation
                    markov_res = run_markov_simulation(page, plan, recorder=recorder)
                    visible_state = {
                        "goal_actions": [{"step": s, "action": s} for s in markov_res.get("state_history", [])],
                        "success": True
                    }
                    logs.append(f"[{datetime.now().isoformat()}] Markov simulation completed. States transitioned: {markov_res.get('steps_executed')}")
                else:
                    visible_state = run_visible_state_goal(
                        page, goal, plan, recorder=recorder
                    )
                from imposter5.automation_connector.goals import goal_spec_to_payload
                goal_payload = goal_spec_to_payload(goal)
                logs.append(f"[{datetime.now().isoformat()}] Simulation completed. Actions recorded: {len(visible_state.get('goal_actions', []))}")

                # If running fp-agent, stop mus recording and get frames
                if body.run_fp_agent:
                    try:
                        from imposter5.fp_agent.fp_agent_local_redteam_detector_test import stop_and_get_mus_frames
                        all_captured_frames = stop_and_get_mus_frames(page)
                        logs.append(f"[{datetime.now().isoformat()}] Stopped mus.js recording. Captured {len(all_captured_frames)} frames")
                    except Exception as e:
                        logs.append(f"[{datetime.now().isoformat()}] Warning: could not stop mus recording: {e}")

                # Close context and browser to finalize movie
                context.close()
                browser.close()
                logs.append(f"[{datetime.now().isoformat()}] Finalized video recording")

            # Copy the movie to desktop and .codex-outputs
            if os.path.isdir(video_dir):
                candidates = []
                for f in os.listdir(video_dir):
                    if f.lower().endswith((".webm", ".mp4")):
                        pth = os.path.join(video_dir, f)
                        try:
                            candidates.append((os.path.getmtime(pth), pth))
                        except Exception:
                            pass
                if candidates:
                    candidates.sort(reverse=True)
                    src = candidates[0][1]
                    
                    home = Path.home()
                    desktop = home / "Desktop"
                    desktop.mkdir(parents=True, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    
                    stamped_desktop = desktop / f"imposter5-movie-{ts}.webm"
                    latest_desktop = desktop / "tokyo-latest-watch-movie.webm"
                    
                    # Standalone output folder
                    codex_dir = Path(backend_dir) / ".codex-outputs"
                    codex_dir.mkdir(parents=True, exist_ok=True)
                    stamped_codex = codex_dir / f"imposter5-{ts}.webm"
                    latest_codex = codex_dir / "latest-watch-movie.webm"
                    
                    shutil.copy2(src, stamped_desktop)
                    shutil.copy2(src, latest_desktop)
                    shutil.copy2(src, stamped_codex)
                    shutil.copy2(src, latest_codex)
                    
                    movie_filename = f"imposter5-{ts}.webm"
                    stamped_codex_path = str(stamped_codex)
                    latest_codex_path = str(latest_codex)
                    logs.append(f"[{datetime.now().isoformat()}] Movie copied to Desktop and .codex-outputs")

        except Exception as e:
            logs.append(f"[{datetime.now().isoformat()}] Error during simulation: {e}")
            if body.provider != "linkedin":
                try:
                    browser.close()
                except Exception:
                    pass

    thread = threading.Thread(target=run_simulation_thread)
    thread.start()
    thread.join()

    # Run fp-agent verdict if requested
    real_verdict = None
    bot_likeness_score = None
    verdict_text = None
    if body.run_fp_agent and all_captured_frames:
        logs.append(f"[{datetime.now().isoformat()}] Running fp-agent evasion detector analysis")
        try:
            from imposter5.fp_agent.fp_agent_local_redteam_detector_test import (
                compute_straightness_and_curvature,
                compute_timing_cv,
                count_overshoot_proxies,
                bot_likeness_score as compute_bot_score,
                BOT_LIKENESS_THRESHOLD,
            )
            mm_only = [f for f in all_captured_frames if f and f[0] == "m"]
            curv = compute_straightness_and_curvature(mm_only)
            t_cv = compute_timing_cv(mm_only)
            overs = count_overshoot_proxies(mm_only)
            bot_likeness_score = compute_bot_score(curv, t_cv, overs, len(mm_only))
            
            if bot_likeness_score < BOT_LIKENESS_THRESHOLD:
                verdict_text = "EVADES (low bot-likeness)"
            else:
                verdict_text = "DETECTED / clusters as bot-like"
                
            real_verdict = _get_fp_agent_verdict(all_captured_frames)
            logs.append(f"[{datetime.now().isoformat()}] fp-agent analysis complete. Heuristic score: {bot_likeness_score:.3f}, Verdict: {verdict_text}")
            if real_verdict:
                logs.append(f"[{datetime.now().isoformat()}] Real model prediction: {real_verdict.get('predicted_label')} (confidence: {real_verdict.get('confidence'):.3f})")
        except Exception as e:
            logs.append(f"[{datetime.now().isoformat()}] Error running fp-agent analysis: {e}")

    # Extract session recording dictionary
    session_rec = None
    if session_recorder_instance:
        try:
            session_rec = session_recorder_instance.payload()
        except Exception:
            pass
    elif posts and isinstance(posts, list) and len(posts) > 0:
        # Extract from LinkedIn scrape posts
        try:
            first_post = posts[0]
            if "extraction_meta" in first_post and "session_recording" in first_post["extraction_meta"]:
                session_rec = first_post["extraction_meta"]["session_recording"]
        except Exception:
            pass

    return success_response({
        "success": True,
        "plan": plan,
        "goal": goal_payload,
        "movie_filename": movie_filename,
        "movie_url": f"/static/.codex-outputs/{movie_filename}" if movie_filename else "",
        "stamped_codex_path": stamped_codex_path,
        "latest_codex_path": latest_codex_path,
        "bot_likeness_score": bot_likeness_score,
        "verdict": verdict_text,
        "real_verdict": real_verdict,
        "logs": logs,
        "session_recording": session_rec,
    })


@app.post("/api/imposter5/login/start")
def api_login_start(body: LoginStartRequest):
    """Start a login session for a user and website URL."""
    from imposter5.automation_connector.login_manager import start_login_session
    try:
        res = start_login_session(body.user_id, body.url, body.mode)
        if res.get("success"):
            return success_response(res)
        return error_response("login_start_failed", res.get("error", "Unknown error"))
    except Exception as e:
        return error_response("login_start_error", str(e))


@app.post("/api/imposter5/login/verify")
def api_login_verify(body: LoginVerifyRequest):
    """Verify a login session and save credentials if successful."""
    from imposter5.automation_connector.login_manager import verify_login_session
    try:
        res = verify_login_session(body.user_id, body.url)
        if res.get("success"):
            return success_response(res)
        return error_response("login_verify_failed", res.get("error", "Unknown error"))
    except Exception as e:
        return error_response("login_verify_error", str(e))


@app.get("/api/imposter5/login/cookies")
def api_login_get_cookies(user_id: str, url: str):
    """Get saved cookies for a user and website URL."""
    from imposter5.automation_connector.login_manager import load_site_cookies
    try:
        cookies = load_site_cookies(user_id, url)
        return success_response({"cookies": cookies})
    except Exception as e:
        return error_response("login_get_cookies_error", str(e))


@app.post("/api/imposter5/login/cookies")
def api_login_set_cookies(body: CookieSetRequest):
    """Save cookies for a user and website URL."""
    from imposter5.automation_connector.login_manager import save_site_cookies
    try:
        save_site_cookies(body.user_id, body.url, body.cookies)
        return success_response({"success": True, "message": "Cookies saved successfully."})
    except Exception as e:
        return error_response("login_set_cookies_error", str(e))


@app.delete("/api/imposter5/login/cookies")
def api_login_delete_cookies(user_id: str, url: str):
    """Delete saved cookies for a user and website URL."""
    from imposter5.automation_connector.login_manager import delete_site_cookies
    try:
        delete_site_cookies(user_id, url)
        return success_response({"success": True, "message": "Cookies deleted successfully."})
    except Exception as e:
        return error_response("login_delete_cookies_error", str(e))


# Serve static recorded movies
@app.get("/static/.codex-outputs/{filename}")
async def get_recorded_movie(filename: str):
    path = Path(backend_dir) / ".codex-outputs" / filename
    if path.is_file():
        return FileResponse(path)
    return HTMLResponse("Not Found", status_code=404)


# Serve built React frontend
@app.get("/{path:path}")
async def serve_frontend(path: str):
    frontend_dist = Path(backend_dir).parent / "frontend" / "dist"
    if path and (frontend_dist / path).is_file():
        return FileResponse(frontend_dist / path)
    
    index_html = frontend_dist / "index.html"
    if index_html.is_file():
        return FileResponse(index_html)
    
    return HTMLResponse("Imposter5 API is running. Build the frontend to view the UI.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5185)
