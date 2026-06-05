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
                    "interaction_style": p.interaction_style,
                }
                for p in PERSONAS
            ]
        })
    except Exception as e:
        return error_response("personas_error", str(e))


@app.post("/api/imposter5/run")
async def imposter5_run(body: Imposter5RunRequest):
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

    # 4. Apply custom variations
    if body.variations:
        plan.setdefault("variations", {}).update(body.variations)

    # 5. Apply custom human_config
    if body.human_config:
        os.environ["AUTOMATION_CONNECTOR_HUMAN_CONFIG"] = json.dumps(body.human_config)
    else:
        os.environ.pop("AUTOMATION_CONNECTOR_HUMAN_CONFIG", None)

    # 6. Set up video directory
    video_dir = tempfile.mkdtemp(prefix="tokyo-imposter5-movie-")
    
    # 7. Launch browser
    runner = get_browser_runner()
    browser = runner.launch_browser(headless=False)
    
    logs = []
    logs.append(f"[{datetime.now().isoformat()}] Starting Imposter5 simulation on {body.url}")
    logs.append(f"[{datetime.now().isoformat()}] Provider: {body.provider}, Persona: {plan.get('persona', {}).get('name')}")
    logs.append(f"[{datetime.now().isoformat()}] Completion: {plan.get('completion')}")
    logs.append(f"[{datetime.now().isoformat()}] Variations: {json.dumps(plan.get('variations'))}")

    all_captured_frames = []
    movie_filename = ""
    stamped_codex_path = ""
    latest_codex_path = ""

    try:
        from imposter5.loaders.cloak_runtime import (
            apply_anti_fingerprint_init_script,
            automation_connector_stealth_context_kwargs,
        )

        ctx_kwargs = automation_connector_stealth_context_kwargs()
        ctx_kwargs["record_video_dir"] = video_dir
        ctx_kwargs["record_video_size"] = {"width": 1440, "height": 900}
        
        context = browser.new_context(**ctx_kwargs)
        try:
            apply_anti_fingerprint_init_script(context)
        except Exception:
            pass
        context.set_default_timeout(25_000)
        page = context.new_page()

        # Enable visible mouse tracking
        enable_visible_mouse_tracking(page)
        logs.append(f"[{datetime.now().isoformat()}] Injected red synthetic HUMAN MOUSE cursor overlay")

        try:
            page.bring_to_front()
        except Exception:
            pass

        # Calibration glide path
        logs.append(f"[{datetime.now().isoformat()}] Running initial 6-point calibration path")
        for cx, cy in [(180, 160), (420, 190), (260, 320), (520, 240), (310, 290), (450, 310)]:
            page.mouse.move(cx, cy)
            page.wait_for_timeout(120)

        # If running fp-agent, start mus recording
        if body.run_fp_agent:
            logs.append(f"[{datetime.now().isoformat()}] Starting mus.js behavioral recording for fp-agent analysis")
            try:
                from imposter5.fp_agent.fp_agent_local_redteam_detector_test import ensure_mus_recording
                ensure_mus_recording(page)
            except Exception as e:
                logs.append(f"[{datetime.now().isoformat()}] Warning: could not start mus recording: {e}")

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
        else:
            logs.append(f"[{datetime.now().isoformat()}] Navigating to target URL: {body.url}")
            page.goto(body.url, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)

            recorder = SessionRecorder(plan)
            goal = goal_spec_from_payload(
                {"goal": "observe_visible_page_state", "completion": "skim_visible_feed"},
                fallback_start_url=body.url,
            )
            logs.append(f"[{datetime.now().isoformat()}] Running generic skim goal actions")
            visible_state = run_visible_state_goal(
                page, goal, plan, recorder=recorder
            )
            logs.append(f"[{datetime.now().isoformat()}] Skim completed. Actions recorded: {len(visible_state.get('goal_actions', []))}")

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
        try:
            browser.close()
        except Exception:
            pass

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

    return success_response({
        "success": True,
        "plan": plan,
        "movie_filename": movie_filename,
        "movie_url": f"/static/.codex-outputs/{movie_filename}" if movie_filename else "",
        "stamped_codex_path": stamped_codex_path,
        "latest_codex_path": latest_codex_path,
        "bot_likeness_score": bot_likeness_score,
        "verdict": verdict_text,
        "real_verdict": real_verdict,
        "logs": logs,
    })


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
    uvicorn.run(app, host="127.0.0.1", port=5180)
