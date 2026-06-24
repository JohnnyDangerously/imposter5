from __future__ import annotations

import os
import sys
import json
import time
import shutil
import tempfile
import logging
from pathlib import Path
from datetime import datetime
from typing import Any

from fastapi import FastAPI
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

# Enable CORS for local development. Note: the wildcard origin "*" is invalid
# together with allow_credentials=True (browsers reject it), so we pin explicit
# local origins and keep credentials enabled.
ALLOWED_ORIGINS = [
    "http://localhost:5185",
    "http://127.0.0.1:5185",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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


class Imposter5RunRequestWithMatrix(Imposter5RunRequest):
    """Run request extended with an optional custom semi-Markov transition matrix.

    Defined here (rather than in the shared models module) so app.py can thread a
    user-supplied matrix into ``plan["markov_matrix"]`` for the simulator.
    """

    markov_matrix: dict[str, Any] | None = None


class WebsiteSaveRequest(BaseModel):
    name: str
    url: str
    description: str = ""
    # Red Team Automation profile: a site affordance map ({kind, roles{role:{css[],text[]}},
    # campaign{}}) that tells the engine how to find feed posts / nav / search / results on
    # this site. Optional — when absent, the resolver falls back to the URL-matched built-in
    # (linkedin/gauntlet) or the generic semantic+text cascade.
    automation_profile: dict[str, Any] | None = None


class ScenarioSaveRequest(BaseModel):
    """A reusable, named test scenario for the playback launcher.

    A scenario captures everything needed to re-run a test (prompt, target,
    duration, headless/fp toggles), so the same behavior can be replayed here or
    transferred to the product path. ``prompt`` blank means the default
    goal+Markov journey.
    """
    name: str = Field(max_length=80)
    prompt: str = Field(default="", max_length=1000)
    url: str = Field(default="http://127.0.0.1:5190/gauntlet", max_length=500)
    duration_s: int = Field(default=120, ge=15, le=900)
    headless: bool = True
    run_fp_agent: bool = False
    description: str = Field(default="", max_length=300)


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


SCENARIOS_FILE_PATH = os.path.join(os.path.dirname(__file__), "src", "imposter5", "automation_connector", "scenarios.json")

# Preloaded Blue-gauntlet test scenarios so the launcher is never a blank box.
# A blank ``prompt`` runs the default goal+Markov journey; a prompt drives the
# run instead. Users can save their own on top of these.
DEFAULT_SCENARIOS = [
    {"name": "Default journey (goal + Markov)", "prompt": "", "url": "http://127.0.0.1:5190/gauntlet", "duration_s": 120, "headless": True, "run_fp_agent": False, "description": "Canned multi-minute journey: ambient feed scans, notifications, interest opens — Markov-driven micro-behavior."},
    {"name": "Casual feed browse", "prompt": "casually browse the feed and scroll around using markov", "url": "http://127.0.0.1:5190/gauntlet", "duration_s": 120, "headless": True, "run_fp_agent": False, "description": "Pure ambient Markov scroll/read through the feed."},
    {"name": "Interest hunt: data engineering", "prompt": "browse the feed and open posts about data engineering, then keep scrolling and reading", "url": "http://127.0.0.1:5190/gauntlet", "duration_s": 180, "headless": True, "run_fp_agent": False, "description": "Goal+Markov hybrid: scan, find ICP-relevant posts, open and read them."},
    {"name": "Check notifications", "prompt": "check notifications then return to the feed and keep reading", "url": "http://127.0.0.1:5190/gauntlet", "duration_s": 120, "headless": True, "run_fp_agent": False, "description": "Navigate to notifications and back — exercises cross-surface navigation."},
    {"name": "Research a profile", "prompt": "search for data engineers, open a profile and read it, then go back to the feed", "url": "http://127.0.0.1:5190/gauntlet", "duration_s": 180, "headless": True, "run_fp_agent": False, "description": "Human-interest endgame: search → results scan → profile read → return."},
]


CODEX_OUTPUTS_DIR = Path(backend_dir) / ".codex-outputs"


def _write_session_sidecar(
    *,
    movie_filename: str,
    session_rec: dict[str, Any] | None,
    video_start_offset_ms: int | None,
    run_metadata: dict[str, Any],
) -> str:
    """Persist a run's session recording next to its video as a sidecar JSON.

    For a video ``imposter5-<ts>.webm`` this writes ``imposter5-<ts>.session.json``
    into the same ``.codex-outputs/`` directory so the playback player can replay
    past runs (the event log is otherwise only returned in the HTTP response).
    Returns the sidecar filename, or "" if nothing was written.
    """
    if not movie_filename or not movie_filename.lower().endswith(".webm"):
        return ""
    events = []
    event_count = 0
    if isinstance(session_rec, dict):
        events = session_rec.get("events") or []
        event_count = int(session_rec.get("event_count") or len(events))

    session_filename = movie_filename[: -len(".webm")] + ".session.json"
    payload = {
        "video_filename": movie_filename,
        "events": events,
        "event_count": event_count,
        "run_metadata": run_metadata,
        "created_at": datetime.now().isoformat(),
        # See video_start_offset_ms note in imposter5_run. May be None when the
        # offset could not be measured (e.g. the canned LinkedIn scraper path,
        # whose recorder is created internally); the player then falls back to
        # treating elapsed_ms as video time, which is approximate.
        "video_start_offset_ms": video_start_offset_ms,
    }
    try:
        CODEX_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(CODEX_OUTPUTS_DIR / session_filename, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logger.error(f"[imposter5] could not write session sidecar: {e}")
        return ""
    return session_filename


def _capture_blue_gauntlet_verdict(page: Any) -> dict[str, Any] | None:
    """Trigger the Blue gauntlet's own scorer and return its verdict, or None.

    The gauntlet page exposes ``window.lhhlSubmit()`` (submit the recorded
    session) and ``window.__lhhl_last_report`` (the async score). We poll up to
    ~10s for the report so any run that ends on a ``/gauntlet`` page — whether
    the canned journey or a prompt-driven walk — surfaces an evasion score.
    """
    try:
        page.evaluate("window.lhhlSubmit && window.lhhlSubmit()")
        for _ in range(50):  # poll up to ~10s for the async score
            rep = page.evaluate("window.__lhhl_last_report || null")
            if rep:
                return rep
            page.wait_for_timeout(200)
    except Exception:
        return None
    return None


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


def _automation_profile_for_url(url: str) -> dict[str, Any] | None:
    """The saved website's Red Team Automation profile for this run URL, if any.

    Matches the run URL against saved websites (exact or substring either way) and
    returns the entry's ``automation_profile``. Returns None when no saved site
    carries a custom profile — the resolver then falls back to the URL-matched
    built-in (linkedin/gauntlet) or generic cascade.
    """
    if not url:
        return None
    try:
        for w in load_websites():
            ap = w.get("automation_profile")
            wu = w.get("url") or ""
            if ap and wu and (wu == url or wu in url or url in wu):
                return ap
    except Exception:
        return None
    return None


def _save_websites_to_disk(websites: list[dict[str, Any]]) -> None:
    try:
        os.makedirs(os.path.dirname(WEBSITES_FILE_PATH), exist_ok=True)
        with open(WEBSITES_FILE_PATH, "w") as f:
            json.dump(websites, f, indent=2)
    except Exception:
        pass


def load_scenarios() -> list[dict[str, Any]]:
    try:
        if os.path.exists(SCENARIOS_FILE_PATH):
            with open(SCENARIOS_FILE_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(SCENARIOS_FILE_PATH), exist_ok=True)
        with open(SCENARIOS_FILE_PATH, "w") as f:
            json.dump(DEFAULT_SCENARIOS, f, indent=2)
    except Exception:
        pass
    return DEFAULT_SCENARIOS


def _save_scenarios_to_disk(scenarios: list[dict[str, Any]]) -> None:
    try:
        os.makedirs(os.path.dirname(SCENARIOS_FILE_PATH), exist_ok=True)
        with open(SCENARIOS_FILE_PATH, "w") as f:
            json.dump(scenarios, f, indent=2)
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
        websites.append(body.model_dump(exclude_none=True))
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


@app.get("/api/imposter5/scenarios")
async def get_scenarios():
    """Return saved test scenarios (preloaded presets + user-saved)."""
    try:
        return success_response({"scenarios": load_scenarios()})
    except Exception as e:
        return error_response("scenarios_error", str(e))


@app.post("/api/imposter5/scenarios")
async def save_scenario(body: ScenarioSaveRequest):
    """Save or update a named test scenario (upsert by name)."""
    try:
        scenarios = load_scenarios()
        scenarios = [s for s in scenarios if s.get("name") != body.name]
        scenarios.append(body.model_dump())
        _save_scenarios_to_disk(scenarios)
        return success_response({"success": True, "message": f"Scenario '{body.name}' saved.", "scenarios": scenarios})
    except Exception as e:
        return error_response("save_scenario_error", str(e))


@app.delete("/api/imposter5/scenarios/{name}")
async def delete_scenario(name: str):
    """Delete a saved scenario."""
    try:
        scenarios = [s for s in load_scenarios() if s.get("name") != name]
        _save_scenarios_to_disk(scenarios)
        return success_response({"success": True, "message": f"Scenario '{name}' deleted.", "scenarios": scenarios})
    except Exception as e:
        return error_response("delete_scenario_error", str(e))


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
def imposter5_run(body: Imposter5RunRequestWithMatrix):
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
        # Anchors the time-of-day duration coupling to the identity's own wall
        # clock when supplied (else behavior_policy defaults to a desk-worker tz).
        "timezone": body.schedule_timezone,
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

    # 5. Apply custom human_config into the per-run plan (not os.environ, which
    # would race across concurrent runs). Downstream humanization reads
    # plan["human_config"].
    if body.human_config:
        plan["human_config"] = sanitize_human_config(body.human_config)

    # 5b. Thread an optional custom semi-Markov transition matrix into the plan so
    # the simulator can consume it; providing one implies Markov pathing.
    if body.markov_matrix:
        plan["markov_matrix"] = body.markov_matrix
        plan["use_markov_pathing"] = True

    # 5c. Thread the site's Red Team Automation profile (affordance map) into the
    # plan so feed behaviors resolve roles on THIS site. The resolver falls back to
    # the URL-matched built-in (linkedin/gauntlet) or generic cascade when a saved
    # site carries no custom profile, so this is purely additive.
    plan["url"] = body.url
    _site_profile = _automation_profile_for_url(body.url)
    if _site_profile:
        plan["automation_profile"] = _site_profile

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
    feasibility_result = None
    linkedin_posts_result = None
    # Distinguishes a genuinely failed canned LinkedIn scrape from a clean run so
    # the response can report honestly instead of always claiming success.
    linkedin_scrape_error = None
    # Blue Team gauntlet run: the journey summary + the Blue evasion verdict
    # (evasion_score / verdict / journey_verdict) captured from the gauntlet's own
    # scorer, surfaced in the imposter session alongside the fp-agent verdict.
    blue_report = None
    gauntlet_summary = None
    gauntlet_error = None
    # Offset (ms) between the event-log clock (SessionRecorder.elapsed_ms, which
    # starts when the recorder is constructed) and the video clock (which starts
    # when Playwright begins capturing the page). The player maps event time to
    # video time via: videoTime = (event.elapsed_ms - video_start_offset_ms)/1000.
    # It is normally negative because the video begins capturing several seconds
    # before the recorder is created (browser launch + initial nav + settle wait).
    video_start_offset_ms = None

    # Compile the prompt into a goal up-front so the pre-run pipeline (auth gate,
    # feasibility review) can reason about the actual requested steps. When there
    # is no prompt, each provider's default goal is resolved inside the run.
    precompiled_goal = None
    if body.prompt:
        from imposter5.automation_connector.goals import (
            derive_plan_overrides,
            goal_spec_from_natural_prompt,
        )
        precompiled_goal = goal_spec_from_natural_prompt(
            body.prompt, start_url=body.url, provider_hint=body.provider
        )
        # Bridge the compiled prompt into the behavior plan: ambient prompts engage
        # the semi-Markov walk and parsed interest terms drive the goal+Markov
        # hybrid's "open the post I care about" behavior. An explicitly-uploaded
        # markov_matrix (set above) wins over the derived scan matrix.
        overrides = derive_plan_overrides(precompiled_goal)
        if overrides.get("use_markov_pathing"):
            plan["use_markov_pathing"] = True
        if overrides.get("interest_terms"):
            plan["interest_terms"] = overrides["interest_terms"]
        if overrides.get("markov_matrix") and not plan.get("markov_matrix"):
            plan["markov_matrix"] = overrides["markov_matrix"]

    # Pipeline seam 1 — credential gate (workstream B). Runs before any browser is
    # opened: if the task needs credentials that are not ready, stop and tell the UI
    # to collect them rather than launching a doomed run.
    from imposter5.automation_connector.auth_gate import evaluate_auth
    auth_decision = evaluate_auth(
        provider=body.provider, url=body.url, prompt=body.prompt, goal=precompiled_goal
    )
    if auth_decision.blocks_run:
        logs.append(
            f"[{datetime.now().isoformat()}] Run needs credentials ({auth_decision.reason}); "
            "prompting user before execution."
        )
        return success_response({
            "success": False,
            "status": "needs_auth",
            "auth": auth_decision.to_payload(),
            "plan": plan,
            "logs": logs,
        })

    def run_simulation_thread():
        nonlocal all_captured_frames, movie_filename, stamped_codex_path, latest_codex_path, goal_payload, session_recorder_instance, posts, feasibility_result, linkedin_posts_result, video_start_offset_ms, linkedin_scrape_error, blue_report, gauntlet_summary, gauntlet_error
        import asyncio
        # Initialize up front so the error path can never NameError / leak a handle.
        browser = None
        # Monotonic timestamp captured at page creation, i.e. when Playwright
        # starts writing the video. Compared against the recorder's start to
        # derive video_start_offset_ms once the recorder exists.
        video_capture_start_monotonic = None
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
            # Run the actual goal. A user prompt is honored on EVERY provider: it
            # falls through to the prompt-driven goal runner below. The canned
            # provider paths (LinkedIn feed scrape, Wikipedia sim) are only the
            # no-prompt defaults.
            if body.provider == "linkedin" and not body.prompt:
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
                        run_fp_agent=body.run_fp_agent,
                    )
                    logs.append(f"[{datetime.now().isoformat()}] LinkedIn feed scrape completed, found {len(posts)} posts")
                    # Surface the scraped posts + hybrid run metadata to the product.
                    linkedin_posts_result = posts or None
                    first_meta = (posts[0].get("extraction_meta") if posts else {}) or {}
                    if first_meta.get("video_start_offset_ms") is not None:
                        video_start_offset_ms = first_meta.get("video_start_offset_ms")
                    # FP-agent frames captured inside the scrape session → verdict.
                    if body.run_fp_agent:
                        frames = first_meta.get("fp_frames")
                        if frames:
                            all_captured_frames = frames
                            logs.append(f"[{datetime.now().isoformat()}] Captured {len(frames)} mus.js frames on the scrape path")
                    # Drop the heavy frames from the payload sent to the client.
                    if posts and "fp_frames" in first_meta:
                        trimmed = dict(first_meta)
                        trimmed.pop("fp_frames", None)
                        posts[0] = {**posts[0], "extraction_meta": trimmed}
                        linkedin_posts_result = posts
                except Exception as exc:
                    linkedin_scrape_error = str(exc)
                    logs.append(f"[{datetime.now().isoformat()}] LinkedIn scrape raised: {exc}")
            elif "/gauntlet" in body.url.lower() and not body.prompt:
                # Blue Team gauntlet run: drive the full improved Red suite
                # (Markov-continuity feed scan + interest-driven search/profile
                # reads + notifications + glances) for a multi-minute session,
                # record video like a LinkedIn run, then trigger the gauntlet's
                # own scorer and capture the Blue evasion verdict.
                gauntlet_headless = body.headless if body.headless is not None else False
                logs.append(f"[{datetime.now().isoformat()}] Executing Blue Team gauntlet journey (headless={gauntlet_headless})")
                runner = get_browser_runner()
                browser = runner.launch_browser(headless=gauntlet_headless)

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
                video_capture_start_monotonic = time.monotonic()

                page.on("console", lambda msg: print(f"[BROWSER CONSOLE] {msg.text}", flush=True))
                page.on("pageerror", lambda exc: print(f"[BROWSER EXCEPTION] {exc}", flush=True))
                enable_visible_mouse_tracking(page)
                try:
                    page.bring_to_front()
                except Exception:
                    pass

                if body.run_fp_agent:
                    try:
                        from imposter5.fp_agent.fp_agent_local_redteam_detector_test import ensure_mus_recording
                        ensure_mus_recording(page)
                    except Exception as e:
                        logs.append(f"[{datetime.now().isoformat()}] Warning: could not start mus recording: {e}")

                logs.append(f"[{datetime.now().isoformat()}] Navigating to gauntlet: {body.url}")
                page.goto(body.url, wait_until="domcontentloaded")
                page.wait_for_timeout(1000)

                recorder = SessionRecorder(plan)
                session_recorder_instance = recorder
                if video_capture_start_monotonic is not None:
                    video_start_offset_ms = round(
                        (video_capture_start_monotonic - recorder.started_monotonic) * 1000
                    )

                try:
                    from imposter5.loaders.gauntlet_journey import run_gauntlet_journey
                    # build_behavior_plan already drew a human, time-of-day-coupled
                    # session length into plan["gauntlet_duration_s"] (see
                    # behavior_policy); an explicit request value still overrides it.
                    duration_s = float(body.gauntlet_duration_s or plan.get("gauntlet_duration_s") or 240.0)
                    # App-supplied "tree of value": queued human side-quests that
                    # take priority over the ambient excursion menu.
                    if body.lookup_people:
                        plan["lookup_people"] = body.lookup_people
                    if body.excursion_queue:
                        plan["excursion_queue"] = body.excursion_queue
                    if body.long_browse:
                        plan["long_browse"] = body.long_browse
                    gauntlet_summary = run_gauntlet_journey(
                        page, plan, recorder=recorder, duration_s=duration_s
                    )
                    logs.append(
                        f"[{datetime.now().isoformat()}] Gauntlet journey done in "
                        f"{gauntlet_summary.get('duration_s')}s "
                        f"(profiles={gauntlet_summary.get('profiles_opened')}, "
                        f"notifs={gauntlet_summary.get('notifications_visited')}, "
                        f"scans={gauntlet_summary.get('feed_scan_bursts')})"
                    )
                except Exception as exc:
                    gauntlet_error = str(exc)
                    logs.append(f"[{datetime.now().isoformat()}] Gauntlet journey raised: {exc}")

                if body.run_fp_agent:
                    try:
                        from imposter5.fp_agent.fp_agent_local_redteam_detector_test import stop_and_get_mus_frames
                        all_captured_frames = stop_and_get_mus_frames(page)
                        logs.append(f"[{datetime.now().isoformat()}] Captured {len(all_captured_frames)} mus.js frames")
                    except Exception as e:
                        logs.append(f"[{datetime.now().isoformat()}] Warning: could not stop mus recording: {e}")

                # Trigger the gauntlet's own telemetry submit + capture the Blue
                # evasion verdict (the "much better stats" than a live run).
                blue_report = _capture_blue_gauntlet_verdict(page)
                if blue_report:
                    logs.append(
                        f"[{datetime.now().isoformat()}] Blue verdict: evasion_score="
                        f"{blue_report.get('evasion_score')}% [{blue_report.get('verdict')}] "
                        f"journey={blue_report.get('journey_verdict')}"
                    )
                else:
                    logs.append(f"[{datetime.now().isoformat()}] Blue verdict not returned (submit may have failed)")

                context.close()
                browser.close()
                logs.append(f"[{datetime.now().isoformat()}] Finalized gauntlet video recording")
            elif "wikipedia.org" in body.url.lower() and not body.prompt:
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
                try:
                    apply_anti_fingerprint_init_script(context)
                except Exception:
                    pass
                context.set_default_timeout(25_000)
                page = context.new_page()
                # Video capture begins at page creation; mark it for clock alignment.
                video_capture_start_monotonic = time.monotonic()

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
                if video_capture_start_monotonic is not None:
                    video_start_offset_ms = round(
                        (video_capture_start_monotonic - recorder.started_monotonic) * 1000
                    )
                from imposter5.loaders.wikipedia_simulator import run_wikipedia_simulation
                run_wikipedia_simulation(page, plan, recorder=recorder)
                logs.append(f"[{datetime.now().isoformat()}] Wikipedia simulation completed.")

                # Close context and browser to finalize movie
                context.close()
                browser.close()
                logs.append(f"[{datetime.now().isoformat()}] Finalized video recording")
            else:
                # 7. Launch browser for prompt-driven web simulation. A LinkedIn
                # target (provider or any linkedin.com URL) runs in the SAME
                # authenticated persistent context the canned scraper uses (profile
                # dir + restored cookies), so a prompt-driven LinkedIn run executes
                # signed in instead of hitting the login wall. Every other target
                # uses a fresh stealth context.
                from imposter5.loaders.cloak_runtime import (
                    apply_anti_fingerprint_init_script,
                    automation_connector_stealth_context_kwargs,
                )

                ctx_kwargs = automation_connector_stealth_context_kwargs()
                ctx_kwargs["record_video_dir"] = video_dir
                ctx_kwargs["record_video_size"] = {"width": 1440, "height": 900}

                is_linkedin_target = body.provider == "linkedin" or "linkedin.com" in body.url.lower()
                if is_linkedin_target:
                    from imposter5.loaders.cloak_runtime import launch_automation_persistent_context
                    from imposter5.loaders.linkedin_browser import linkedin_profile_dir, load_cookies
                    from imposter5.automation_connector.auth_gate import RUN_USER_ID

                    browser = None  # persistent context owns its own browser process
                    context = launch_automation_persistent_context(
                        linkedin_profile_dir(RUN_USER_ID), headless=False, **ctx_kwargs
                    )
                    try:
                        apply_anti_fingerprint_init_script(context)
                    except Exception:
                        pass
                    context.set_default_timeout(25_000)
                    restored = load_cookies(RUN_USER_ID)
                    if restored:
                        try:
                            context.add_cookies(restored)
                            logs.append(f"[{datetime.now().isoformat()}] Restored {len(restored)} stored LinkedIn session cookies")
                        except Exception as e:
                            logs.append(f"[{datetime.now().isoformat()}] Warning: could not restore LinkedIn cookies: {e}")
                    pages = getattr(context, "pages", None) or []
                    page = pages[0] if pages else context.new_page()
                    # Video capture begins at page creation; mark it for clock alignment.
                    video_capture_start_monotonic = time.monotonic()
                else:
                    runner = get_browser_runner()
                    # Honor an explicit headless request (e.g. a prompt test kicked
                    # off from the playback tool on a server-started backend with no
                    # desktop session); default to visible for the product UI.
                    prompt_headless = body.headless if body.headless is not None else False
                    browser = runner.launch_browser(headless=prompt_headless)
                    context = browser.new_context(**ctx_kwargs)
                    try:
                        apply_anti_fingerprint_init_script(context)
                    except Exception:
                        pass
                    context.set_default_timeout(25_000)
                    page = context.new_page()
                    # Video capture begins at page creation; mark it for clock alignment.
                    video_capture_start_monotonic = time.monotonic()

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

                if is_linkedin_target:
                    try:
                        from imposter5.loaders.linkedin_browser import is_logged_in as _li_logged_in
                        if _li_logged_in(page):
                            logs.append(f"[{datetime.now().isoformat()}] LinkedIn session authenticated")
                        else:
                            logs.append(f"[{datetime.now().isoformat()}] WARNING: LinkedIn not authenticated; prompt run may hit the login wall")
                    except Exception:
                        pass

                recorder = SessionRecorder(plan)
                session_recorder_instance = recorder
                if video_capture_start_monotonic is not None:
                    video_start_offset_ms = round(
                        (video_capture_start_monotonic - recorder.started_monotonic) * 1000
                    )
                if body.prompt:
                    # Reuse the goal compiled up-front for the pre-run pipeline so
                    # the feasibility review and the executor act on the same steps.
                    goal = precompiled_goal
                    logs.append(f"[{datetime.now().isoformat()}] Interpreting natural-language prompt: {body.prompt}")
                    logs.append(f"[{datetime.now().isoformat()}] Compiled prompt into goal: {goal.name} with {len(goal.steps)} steps")
                else:
                    goal = goal_spec_from_payload(
                        {"goal": "observe_visible_page_state", "completion": "skim_visible_feed"},
                        fallback_start_url=body.url,
                    )
                    logs.append(f"[{datetime.now().isoformat()}] Running generic skim goal actions")

                # Pipeline seam 2 — feasibility / action review (workstream C). The
                # page is open, so we can confirm each required step is doable before
                # executing; an infeasible required step stops the run with reasons.
                from imposter5.automation_connector.feasibility import review_feasibility
                feasibility_result = review_feasibility(page, goal, plan)
                if feasibility_result.blocks_run:
                    logs.append(
                        f"[{datetime.now().isoformat()}] Task not possible on this page: "
                        f"{feasibility_result.summary}"
                    )
                    from imposter5.automation_connector.goals import goal_spec_to_payload
                    goal_payload = goal_spec_to_payload(goal)
                    context.close()
                    browser.close()
                    return

                if plan.get("use_markov_pathing") or (body.prompt and "markov" in body.prompt.lower()):
                    logs.append(f"[{datetime.now().isoformat()}] Running dynamic Markov Chain Pathing Simulator")
                    from imposter5.loaders.markov_simulator import run_markov_simulation
                    markov_res = run_markov_simulation(page, plan, recorder=recorder)
                    visible_state = {
                        "goal_actions": [{"step": s, "action": s} for s in markov_res.get("state_history", [])],
                        "success": True
                    }
                    # The ambient Markov scan still serves the goal on LinkedIn:
                    # harvest whatever feed posts the walk scrolled into view so an
                    # ambient prompt ("casually browse LinkedIn") returns evidence
                    # instead of only a state history.
                    if is_linkedin_target:
                        try:
                            from imposter5.loaders.linkedin_feed_scraper import extract_visible_posts
                            visible_state["linkedin_posts"] = extract_visible_posts(page)
                        except Exception as exc:
                            logs.append(f"[{datetime.now().isoformat()}] Markov LinkedIn extraction skipped: {exc}")
                    logs.append(f"[{datetime.now().isoformat()}] Markov simulation completed. States transitioned: {markov_res.get('steps_executed')}")
                else:
                    visible_state = run_visible_state_goal(
                        page, goal, plan, recorder=recorder
                    )
                from imposter5.automation_connector.goals import goal_spec_to_payload
                goal_payload = goal_spec_to_payload(goal)
                linkedin_posts_result = visible_state.get("linkedin_posts") or None
                if linkedin_posts_result:
                    logs.append(f"[{datetime.now().isoformat()}] Extracted {len(linkedin_posts_result)} structured LinkedIn posts during the prompt run")
                logs.append(f"[{datetime.now().isoformat()}] Simulation completed. Actions recorded: {len(visible_state.get('goal_actions', []))}")

                # If running fp-agent, stop mus recording and get frames
                if body.run_fp_agent:
                    try:
                        from imposter5.fp_agent.fp_agent_local_redteam_detector_test import stop_and_get_mus_frames
                        all_captured_frames = stop_and_get_mus_frames(page)
                        logs.append(f"[{datetime.now().isoformat()}] Stopped mus.js recording. Captured {len(all_captured_frames)} frames")
                    except Exception as e:
                        logs.append(f"[{datetime.now().isoformat()}] Warning: could not stop mus recording: {e}")

                # If this prompt run targeted the Blue gauntlet, capture its
                # evasion verdict too so prompt-driven Blue tests show a score.
                if "/gauntlet" in body.url.lower():
                    blue_report = _capture_blue_gauntlet_verdict(page)
                    if blue_report:
                        logs.append(
                            f"[{datetime.now().isoformat()}] Blue verdict (prompt run): "
                            f"{blue_report.get('evasion_score')}% [{blue_report.get('verdict')}] "
                            f"journey={blue_report.get('journey_verdict')}"
                        )

                # Persist refreshed LinkedIn session cookies before teardown (keeps
                # the stored jar warm, like the scraper's session does).
                if is_linkedin_target:
                    try:
                        from imposter5.loaders.linkedin_browser import save_cookies
                        from imposter5.automation_connector.auth_gate import RUN_USER_ID as _RUID
                        cks = context.cookies()
                        if cks:
                            save_cookies(_RUID, cks)
                    except Exception:
                        pass

                # Close context and browser to finalize movie
                context.close()
                if browser is not None:
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
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    thread = threading.Thread(target=run_simulation_thread)
    thread.start()
    thread.join()

    # Feasibility short-circuit: a required step was not possible on the page, so the
    # run stopped before execution. Report the per-step verdicts to the UI.
    if feasibility_result is not None and feasibility_result.blocks_run:
        return success_response({
            "success": False,
            "status": "infeasible",
            "feasibility": feasibility_result.to_payload(),
            "auth": auth_decision.to_payload(),
            "plan": plan,
            "goal": goal_payload,
            "logs": logs,
        })

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

    # A zero-post scrape never lifts the payload and an interrupted run can drop
    # it — fall back to the on-disk live event log (events.jsonl in video_dir) so
    # the sidecar carries the real motor track instead of ``events: []``.
    if not (isinstance(session_rec, dict) and session_rec.get("events")):
        try:
            from imposter5.automation_connector.session_recorder import (
                load_partial_session,
            )
            partial = load_partial_session(video_dir)
        except Exception:
            partial = None
        if partial is not None:
            session_rec = partial
            logs.append(
                f"[{datetime.now().isoformat()}] Recovered {partial['event_count']} "
                "events from events.jsonl (in-memory payload was empty)"
            )

    # Persist the run's session recording to disk as a sidecar JSON next to the
    # video so the /playback player can replay this run later, not just in-process.
    session_filename = _write_session_sidecar(
        movie_filename=movie_filename,
        session_rec=session_rec,
        video_start_offset_ms=video_start_offset_ms,
        run_metadata={
            "prompt": body.prompt,
            "provider": body.provider,
            "url": body.url,
            "persona": plan.get("persona", {}).get("name"),
            # Blue gauntlet verdict + journey stats, when this was a /gauntlet run,
            # so the playback tool can show the evasion score without re-running.
            "blue_report": blue_report,
            "gauntlet_summary": gauntlet_summary,
        },
    )
    if session_filename:
        logs.append(f"[{datetime.now().isoformat()}] Wrote session sidecar {session_filename}")

    # Honest outcome: a canned LinkedIn scrape that raised or returned no posts is
    # a failure, not a silent success. Same for a gauntlet journey that raised.
    scrape_path = body.provider == "linkedin" and not body.prompt
    scrape_failed = scrape_path and (linkedin_scrape_error is not None or not linkedin_posts_result)
    gauntlet_path = "/gauntlet" in body.url.lower() and not body.prompt
    gauntlet_failed = gauntlet_path and (gauntlet_error is not None or gauntlet_summary is None)
    run_success = not (scrape_failed or gauntlet_failed)
    run_status = (
        "scrape_failed" if scrape_failed
        else "gauntlet_failed" if gauntlet_failed
        else "ran"
    )

    # Pipeline seam 3 — first-run verdict + optional scheduling (workstream D).
    from imposter5.automation_connector.scheduler import finalize_run
    run_outcome = finalize_run(
        provider=body.provider,
        url=body.url,
        prompt=body.prompt,
        result={"success": run_success, "goal": goal_payload, "session_recording": session_rec},
        schedule=(
            {
                "interval_minutes": body.schedule_interval_minutes,
                # Thread the run's persona + timezone into the schedule so the
                # enrolled identity gets the persona's chronotype in its own local
                # wall clock, instead of the whole fleet defaulting to one shared
                # nine-to-five Eastern rhythm.
                "persona": body.persona,
                "timezone": body.schedule_timezone,
            }
            if body.schedule_interval_minutes
            else None
        ),
    )

    return success_response({
        "success": run_success,
        "status": run_status,
        "error": linkedin_scrape_error or gauntlet_error,
        "plan": plan,
        "goal": goal_payload,
        "auth": auth_decision.to_payload(),
        "feasibility": feasibility_result.to_payload() if feasibility_result is not None else None,
        "run_outcome": run_outcome.to_payload(),
        "linkedin_posts": linkedin_posts_result,
        "blue_report": blue_report,
        "gauntlet_summary": gauntlet_summary,
        "movie_filename": movie_filename,
        "movie_url": f"/static/.codex-outputs/{movie_filename}" if movie_filename else "",
        "session_url": f"/static/.codex-outputs/{session_filename}" if session_filename else "",
        "video_start_offset_ms": video_start_offset_ms,
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


# --------------------------------------------------------------------------- #
# Scheduler worker wiring (workstream D, armed by the integrator)
# --------------------------------------------------------------------------- #
def _scheduled_run(provider: str, url: str, prompt: str | None) -> dict:
    """Re-launch a due scheduled task through the normal run path.

    Returns the run-result shape the scheduler's verdict logic expects. The
    re-run carries no schedule, so it finalizes verdict-only and never
    re-enrolls itself — the worker owns rescheduling.
    """
    import json as _json
    try:
        resp = imposter5_run(Imposter5RunRequestWithMatrix(provider=provider, url=url, prompt=prompt))
        body = _json.loads(resp.body)
    except Exception as e:
        return {"success": False, "reason": f"scheduled run failed: {e}"}
    return {
        "success": bool(body.get("success", False)),
        "goal": body.get("goal"),
        "session_recording": body.get("session_recording"),
    }


_scheduler_handle = None


@app.on_event("startup")
def _start_scheduler_worker() -> None:
    """Register the run callable and start the recurring-task worker.

    Disable with IMPOSTER5_DISABLE_SCHEDULER=1; tune cadence with
    IMPOSTER5_SCHEDULER_POLL_SECONDS. Not started during plain ``import app``.
    """
    global _scheduler_handle
    if os.environ.get("IMPOSTER5_DISABLE_SCHEDULER"):
        return
    from imposter5.automation_connector.scheduler import set_run_callable, start_worker
    set_run_callable(_scheduled_run)
    poll = float(os.environ.get("IMPOSTER5_SCHEDULER_POLL_SECONDS", "60"))
    _scheduler_handle = start_worker(poll_seconds=poll)


@app.on_event("shutdown")
def _stop_scheduler_worker() -> None:
    global _scheduler_handle
    if _scheduler_handle is not None:
        from imposter5.automation_connector.scheduler import stop_worker
        stop_worker(_scheduler_handle)
        _scheduler_handle = None


# --------------------------------------------------------------------------- #
# Loom-style session playback
# --------------------------------------------------------------------------- #
PLAYBACK_PLAYER_PATH = Path(backend_dir) / "static" / "playback.html"


def _summarize_events(events: list[dict[str, Any]]) -> str:
    """Build a short human summary of a run from its event log."""
    if not events:
        return "No recorded events"
    actions = [str(e.get("action") or "") for e in events]
    first_goto = next(
        (e.get("metadata", {}).get("url") for e in events if e.get("action") == "goto"),
        None,
    )
    distinct = []
    for a in actions:
        if a and a not in distinct:
            distinct.append(a)
    head = first_goto or (distinct[0] if distinct else "session")
    return f"{head} — {len(events)} events ({', '.join(distinct[:4])})"


def _safe_run_id(run_id: str) -> bool:
    return bool(run_id) and all(c.isalnum() or c in "-_" for c in run_id)


def _list_playback_runs() -> list[dict[str, Any]]:
    """Scan .codex-outputs/ for *.session.json with a matching *.webm, newest first."""
    runs: list[dict[str, Any]] = []
    if not CODEX_OUTPUTS_DIR.is_dir():
        return runs
    for session_path in CODEX_OUTPUTS_DIR.glob("*.session.json"):
        run_id = session_path.name[: -len(".session.json")]
        try:
            data = json.loads(session_path.read_text())
        except Exception:
            continue
        video_filename = data.get("video_filename") or f"{run_id}.webm"
        if not (CODEX_OUTPUTS_DIR / video_filename).is_file():
            continue
        events = data.get("events") or []
        runs.append({
            "id": run_id,
            "video_url": f"/static/.codex-outputs/{video_filename}",
            "session_url": f"/static/.codex-outputs/{session_path.name}",
            "created_at": data.get("created_at"),
            "event_count": int(data.get("event_count") or len(events)),
            "run_metadata": data.get("run_metadata") or {},
            "summary": _summarize_events(events),
        })
    runs.sort(
        key=lambda r: (r.get("created_at") or "", r.get("id") or ""),
        reverse=True,
    )
    return runs


@app.get("/playback")
async def playback_page():
    """Serve the Loom-style session playback player."""
    if PLAYBACK_PLAYER_PATH.is_file():
        return FileResponse(PLAYBACK_PLAYER_PATH)
    return HTMLResponse("Playback player asset missing", status_code=500)


@app.get("/api/playback/runs")
async def playback_runs():
    """List available recorded runs (newest first)."""
    return success_response({"runs": _list_playback_runs()})


@app.get("/api/playback/runs/{run_id}")
async def playback_run(run_id: str):
    """Return a single run's persisted session JSON."""
    if not _safe_run_id(run_id):
        return error_response("invalid_run_id", "Run id contains illegal characters")
    session_path = CODEX_OUTPUTS_DIR / f"{run_id}.session.json"
    if not session_path.is_file():
        return error_response("run_not_found", f"No session recording for '{run_id}'")
    try:
        data = json.loads(session_path.read_text())
    except Exception as e:
        return error_response("run_read_error", str(e))
    video_filename = data.get("video_filename") or f"{run_id}.webm"
    data["id"] = run_id
    data["video_url"] = f"/static/.codex-outputs/{video_filename}"
    return success_response(data)


# Serve static recorded movies (and their *.session.json sidecars)
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
