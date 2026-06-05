#!/usr/bin/env python3
"""
Working local redteam test: "Does the fp-agent agent detector pick up and cluster our cloak humanize + styled mouse moves as bot behavior?"

This script recreates the full setup so you can answer:
- Does the current "evasion setup" (cloak humanize=True + careful preset + our landed automation_connector behavior_plan pointer styles (two_step/slight_arc) + imprecision/overshoot + expressive human_config for high wobble/steps/bursts) get detected/clustered by fp-agent's classifier?
- Or does it produce behavioral traces (via their own mus.js recorder) that look different from the rigid automation agents in their data_collection (Skyvern, browser-use, etc.)?

Run this, look at the VERDICT and scores.

It:
1. Starts the local honey site (fp-agent/honey_website) in HONEY_TEST_NO_DB mode (our local-only patches; no real DB, auto version, graceful logging).
2. Uses the host's LAN IP + chromium args so the launched cloak browser can actually reach the local honey (fixes the common localhost reachability issue inside instrumented chromium).
3. Launches CloakBrowser with the "evasion" humanize config (or --mode naive for a direct-move baseline with humanize off).
4. On the honey pages (which load their real mus.js for behavioral recording), injects/starts mus recording with timepoints.
5. Drives interactions using OUR production code:
   - server.automation_connector.behavior_policy.build_behavior_plan (the persona/completion/pointer knobs)
   - interaction_primitives.move_pointer (the landed redteam improvement: direct/slight_arc/two_step + imprecision + overshoot, seeded)
   - plus scroll_page, wait_human, click_element etc. (all the bounded human ergonomics)
   This is exactly what real automation_connector runs (Social Inbox, cloak checks, etc.) would produce against a target.
6. Captures the exact mus frames (["m", x, y, t], clicks, etc.) that would be POSTed to their /mouse_movement (or /mm).
7. Forces a POST of the captured frames to the honey endpoint (so the test-mode server logs the precise payload).
8. Computes a simple, transparent "bot-likeness" heuristic detector based on signals that appear in their featurizer (path straightness/curvature stats, timing regularity/CV, overshoot+correction proxies, burstiness). These are cheap approximations of the real behavioral FV features (angles_of_curvature, curvature_distances, inter-event latencies, group_bursts, velocity/accel variance, etc.).
9. Prints clear numbers + VERDICT:
   - High bot-likeness (regular, straight, low-variance, few corrections) => "would likely be clustered with known automation agents and detected".
   - Low (wiggly paths from two_step corrections + high wobble from human_config + bursty/variable timing from cloak human layer + our pacing) => "evades / not clustered as the rigid bots in their training data".
10. Saves the raw frames JSON (timestamped) to /tmp/ so you can feed the exact behavioral data into their full classifier_training/featurizer.py + a trained model later for the real multi-class prediction.

Baseline comparison:
- Run with --mode naive to drive "dumb bot" (fixed-step direct page.mouse.move, no humanize, no our styles, minimal waits). Expect high bot-likeness.
- Default is the "setup we think cannot be detected".

This is the reproducible test harness. Tweak the plan/human_config in the script or via env, re-run, see if the detector score moves.

No changes to fp-agent upstream. All test-only code lives here.

Prereqs (local clone):
- The fp-agent clone at this path (with our local test-mode patches to honey_website/util/*.py and server.py already applied).
- tokyo automation code on PYTHONPATH so we can import the real primitives/plan (this is "our stuff").
- cloakbrowser + camoufox optional for the naive mode etc. installed in the env.
- uv or the venv that has the tokyo extras.

Run (from tokyo root, with the cloak worktree or main that has the landed mouse humanize changes):
  PYTHONPATH=/Users/john/repos/_wt/internal-app-tokyo-cloak-mouse:$PYTHONPATH \
    uv run --extra dev python /Users/john/repos/_wt/fp-agent/fp_agent_local_redteam_detector_test.py \
    --interactions 2 --mode evasion

Or --mode naive for the "detectable" control.

Look for the VERDICT line. Also tail the log it mentions for the exact frames that hit the collector.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from typing import Any

# --- Config / paths (local only) ---
FP_AGENT_ROOT = os.path.dirname(os.path.abspath(__file__))
CLASSIFIER_TRAINING_SRC = os.path.join(FP_AGENT_ROOT, "classifier_training", "src")
if CLASSIFIER_TRAINING_SRC not in sys.path:
    sys.path.insert(0, CLASSIFIER_TRAINING_SRC)

HONEY_DIR = os.path.join(FP_AGENT_ROOT, "honey_website")
MUS_JS = os.path.join(
    HONEY_DIR, "static_sites", "honey_site", "js", "mus.js"
)

# Our production evasion setup (the "stuff we think cannot be detected")
# This uses the landed changes + humanize tuning.
DEFAULT_EVASION_HUMAN_CONFIG = {
    "mouse_wobble_max": 5.5,
    "mouse_max_steps": 140,
    "mouse_overshoot_chance": 0.32,
    "mouse_overshoot_px": (3, 13),
    "mouse_burst_size": (2, 8),
    "mouse_burst_pause": (4, 28),
    "click_aim_delay_button": (40, 210),
}

# Heuristic detector threshold (tune as we learn from real featurizer runs)
BOT_LIKENESS_THRESHOLD = 0.55  # below this = "evades current clusters" for our purposes

# --- Imports from our landed redteam improvements ---
from imposter5.automation_connector.behavior_policy import build_behavior_plan
from imposter5.automation_connector.interaction_primitives import (
    click_element,
    move_pointer,
    scroll_page,
    wait_human,
    enable_visible_mouse_tracking,
)
from imposter5.automation_connector.session_recorder import SessionRecorder

from imposter5.loaders.cloak_runtime import (
    automation_connector_cloak_options,
    automation_connector_locale,
    automation_connector_timezone,
)
from cloakbrowser import launch


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def wait_for_url(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def start_honey(port: int, log_path: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["HONEY_TEST_NO_DB"] = "1"
    env["PORT"] = str(port)

    ver_file = os.path.join(HONEY_DIR, "versions.txt")
    if not os.path.exists(ver_file):
        with open(ver_file, "w") as f:
            f.write("redteamtest1\n")

    server_py = os.path.join(HONEY_DIR, "server.py")
    cmd = [sys.executable, server_py, "--test", "-p", str(port)]
    print("[DETECTOR] starting honey:", " ".join(cmd))
    logf = open(log_path, "a")
    proc = subprocess.Popen(cmd, cwd=HONEY_DIR, env=env, stdout=logf, stderr=logf)
    return proc


def launch_cloak(evasion: bool = True, headless: bool = True) -> Any:
    opts = automation_connector_cloak_options()
    if evasion:
        hc = opts.get("human_config") or {}
        hc.update(DEFAULT_EVASION_HUMAN_CONFIG)
        opts["human_config"] = hc
        humanize_flag = True
        print("[DETECTOR] EVASION mode: cloak humanize + our expressive human_config + styled pointer plan")
    else:
        opts["humanize"] = False
        humanize_flag = False
        print("[DETECTOR] NAIVE mode: humanize disabled, direct moves")

    chromium_args = [
        "--allow-insecure-localhost",
        "--ignore-certificate-errors",
        "--disable-web-security",
    ]
    return launch(
        headless=headless,
        locale=automation_connector_locale(),
        timezone=automation_connector_timezone(),
        args=chromium_args,
        **opts,
    )


def ensure_mus_recording(page: Any) -> None:
    if not os.path.exists(MUS_JS):
        raise RuntimeError(f"mus.js not found at {MUS_JS}")
    with open(MUS_JS) as f:
        mus_src = f.read()

    page.evaluate(
        f"""
        (function() {{
            if (!window.__fp_detector_mus) {{
                {mus_src}
                window.__fp_detector_mus = new Mus();
                window.__fp_detector_mus.setTimePoint(true);
            }}
            window.__fp_detector_mus.record();
            console.log("fp_detector_mus recording (re)started");
            return true;
        }})();
        """
    )


def stop_and_get_mus_frames(page: Any) -> list:
    frames = page.evaluate(
        """
        (function() {
            var m = window.__fp_detector_mus;
            if (!m) return [];
            m.stop();
            var d = m.getData();
            return d.frames || [];
        })();
        """
    )
    return frames


def force_post_mus_frames(page: Any, ip: str, port: int, version: str, frames: list) -> None:
    post_url = f"http://{ip}:{port}/{version}/mouse_movement"
    payload = json.dumps({"frames": frames, "source": "fp_agent_local_redteam_detector"})
    page.evaluate(
        f"""
        fetch("{post_url}", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: {payload!r}
        }}).then(r => console.log("forced mus post status", r.status)).catch(e => console.log("post err", e));
        """
    )
    time.sleep(1.2)


# --- Lightweight detector heuristics (inspired by their BehavioralFV / _process_mm_* etc.) ---
def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def compute_straightness_and_curvature(mm_frames: list) -> dict[str, float]:
    """Approx of their curvature / straightness signals from mm events."""
    if len(mm_frames) < 3:
        return {"straightness": 1.0, "curv_angle_std": 0.0, "curv_dist_mean": 0.0}

    pts = [(float(f[1]), float(f[2])) for f in mm_frames if f and f[0] == "m"]
    if len(pts) < 3:
        return {"straightness": 1.0, "curv_angle_std": 0.0, "curv_dist_mean": 0.0}

    # straightness: end-to-end / path length (1.0 = perfectly straight/rigid)
    path_len = sum(_dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    end_to_end = _dist(pts[0], pts[-1])
    straightness = (end_to_end / path_len) if path_len > 0 else 1.0

    angles = []
    curv_dists = []
    for i in range(len(pts) - 2):
        p1, p2, p3 = pts[i], pts[i + 1], pts[i + 2]
        # angle of curvature (simplified cross product angle)
        v1 = (p2[0] - p1[0], p2[1] - p1[1])
        v2 = (p3[0] - p2[0], p3[1] - p2[1])
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        det = v1[0] * v2[1] - v1[1] * v2[0]
        ang = math.degrees(math.atan2(det, dot))
        angles.append(abs(ang))

        # curvature distance (perp dist from p2 to line p1-p3)
        line_len = _dist(p1, p3)
        if line_len > 1e-6:
            perp = abs((p3[0] - p1[0]) * (p1[1] - p2[1]) - (p1[0] - p2[0]) * (p3[1] - p1[1])) / line_len
            curv_dists.append(perp)

    return {
        "straightness": straightness,
        "curv_angle_std": (sum((a - (sum(angles) / len(angles))) ** 2 for a in angles) / len(angles)) ** 0.5 if angles else 0.0,
        "curv_dist_mean": sum(curv_dists) / len(curv_dists) if curv_dists else 0.0,
    }


def compute_timing_cv(mm_frames: list) -> float:
    """Coefficient of variation of inter-mm times. Low = very regular = bot-like."""
    times = [float(f[3]) for f in mm_frames if f and f[0] == "m" and len(f) > 3]
    if len(times) < 3:
        return 0.0
    deltas = [times[i + 1] - times[i] for i in range(len(times) - 1) if times[i + 1] > times[i]]
    if not deltas:
        return 0.0
    mean_d = sum(deltas) / len(deltas)
    if mean_d <= 0:
        return 0.0
    var = sum((d - mean_d) ** 2 for d in deltas) / len(deltas)
    std = var ** 0.5
    return std / mean_d  # high CV = bursty / variable = more human


def count_overshoot_proxies(mm_frames: list, window: int = 5) -> int:
    """Crude proxy for overshoot + correction (direction reversal shortly after a move)."""
    pts = [(f[1], f[2]) for f in mm_frames if f and f[0] == "m"]
    if len(pts) < window + 1:
        return 0
    count = 0
    for i in range(len(pts) - window):
        dx1 = pts[i + 1][0] - pts[i][0]
        dy1 = pts[i + 1][1] - pts[i][1]
        for j in range(1, min(window, len(pts) - i - 1)):
            dx2 = pts[i + 1 + j][0] - pts[i + j][0]
            dy2 = pts[i + 1 + j][1] - pts[i + j][1]
            if dx1 * dx2 < -0.1 and dy1 * dy2 < -0.1:  # rough opposite direction
                count += 1
                break
    return count


def bot_likeness_score(curv: dict, timing_cv: float, overshoots: int, n_moves: int) -> float:
    """0.0 = very human-like / evasive; 1.0 = rigid bot-like."""
    straight = curv.get("straightness", 1.0)
    curv_std = curv.get("curv_angle_std", 0.0)
    curv_mean = curv.get("curv_dist_mean", 0.0)

    # straightness pulls toward bot
    s_term = straight
    # high curvature variance + mean pulls toward human (evasion)
    c_term = min(1.0, (curv_std / 40.0) + (curv_mean / 30.0))
    # high timing CV = human
    t_term = min(1.0, timing_cv / 1.8)
    # overshoots / corrections (normalized by move count) = human
    o_term = min(1.0, overshoots / max(1, n_moves * 0.3))

    # weighted; tune as needed
    score = 0.45 * s_term + 0.25 * (1 - c_term) + 0.15 * (1 - t_term) + 0.15 * (1 - o_term)
    return max(0.0, min(1.0, score))


# --- Real fp-agent classifier verdict (the "tool's verdict") ---
# This uses their exact BehavioralFV + preprocess + a pre-trained behavioral model from their classifiers/.
# It answers "what does fp-agent's own trained detector actually output for these frames?"

_BROWSING_AGENTS_ONLY_LABELS = [
    # alphabetical sort order used by their create_label_mapping when Human is removed
    "Atlas Agent",
    "Browser Use",
    "ChatGPT Agent",
    "Claude",
    "Comet",
    "Manus",
    "Skyvern",
]

def _normalize_mus_to_fpagent_events(raw_frames: list) -> list:
    """Map mus.js frames (type 'm' for move, 's' for start markers etc.) to the canonical
    event tuples their BehavioralFV + preprocess_tuple expect ("mm", "md", etc. with element id slot).
    """
    from classifier_training.data_preprocessing import preprocess_tuple  # type: ignore

    normalized = []
    for ev in raw_frames:
        if not ev:
            continue
        et = ev[0]
        if et == "m":
            # 4-elem mus move -> will become 5-elem "mm" after preprocess inserts N/A at pos 1
            normalized.append(["mm"] + ev[1:])
        elif et == "s":
            # session start marker from mus; not a behavioral event for their FV
            continue
        else:
            normalized.append(list(ev))
    events = []
    for e in normalized:
        try:
            events.append(list(preprocess_tuple(tuple(e))))
        except Exception:
            # skip malformed
            pass
    return events


def try_real_fp_agent_verdict(frames: list, *, print_header: bool = True) -> dict | None:
    """Attempt to run the *actual* fp-agent behavioral classifier on the given mus frames list.
    Returns a dict with the verdict or None if the fp-agent training package / model / features
    are not importable in this python env (e.g. wrong PROJECT_ROOT, no xgboost, features/*.txt missing).
    When successful, also prints a clear "FP-AGENT REAL VERDICT" block.
    """
    result = None
    try:
        import os
        import numpy as np  # type: ignore

        # Ensure PROJECT_ROOT points at the classifier_training tree so feature_index can find features/*.txt
        # The user (or CI) can also set it externally.
        if not os.environ.get("PROJECT_ROOT"):
            # Heuristic: if we are inside the fp-agent checkout, point at the subdir that contains features/
            guess = os.path.dirname(os.path.abspath(__file__))
            if os.path.exists(os.path.join(guess, "classifier_training", "features")):
                os.environ["PROJECT_ROOT"] = os.path.join(guess, "classifier_training")
            elif os.path.exists(os.path.join(guess, "features")):
                os.environ["PROJECT_ROOT"] = guess

        # The import must succeed with the features visible.
        from classifier_training.featurizer import BehavioralFV  # type: ignore
        from classifier_training.common import load_model  # type: ignore

        events = _normalize_mus_to_fpagent_events(frames)
        if not events:
            print("[REAL FP-AGENT VERDICT] no usable behavioral events after normalization")
            return None

        fv = BehavioralFV()
        fv.parse_events(events)
        behavioral_vec = fv.extract_feature_vector()

        # Use the behavioral-only model trained on the known automation agents (no Human).
        # This is the most direct for "does our behavior look like one of their collected FP agents?"
        model_path = os.path.join(
            os.environ["PROJECT_ROOT"], "classifiers", "behavioral_fingerprint_browsing_agents_only.json"
        )
        if not os.path.exists(model_path):
            print("[REAL FP-AGENT VERDICT] model not found at", model_path)
            return None

        model = load_model(model_path)
        X = np.asarray([behavioral_vec], dtype=float)
        pred_idx = int(model.predict(X)[0])
        proba = None
        try:
            proba = [float(p) for p in model.predict_proba(X)[0]]
        except Exception:
            pass

        label = _BROWSING_AGENTS_ONLY_LABELS[pred_idx] if 0 <= pred_idx < len(_BROWSING_AGENTS_ONLY_LABELS) else f"unknown-{pred_idx}"
        top_p = max(proba) if proba else None

        result = {
            "model": "behavioral_fingerprint_browsing_agents_only",
            "predicted_index": pred_idx,
            "predicted_label": label,
            "confidence": top_p,
            "all_proba": proba,
            "behavioral_vec_len": len(behavioral_vec),
        }

        if print_header:
            print("\n=== FP-AGENT *REAL* CLASSIFIER VERDICT (their trained model on our captured mus frames) ===")
            print(f"Model: {result['model']}")
            print(f"Predicted class: {label} (index {pred_idx})")
            if top_p is not None:
                print(f"Confidence: {top_p:.3f}")
            if proba:
                print("Per-class probs (Atlas, Browser Use, ChatGPT, Claude, Comet, Manus, Skyvern):")
                print(" ", [round(p, 4) for p in proba])
            print("This is the output of fp-agent's own XGBoost behavioral classifier (not our heuristic).")
            print("If it picks one of the known automation agent labels (instead of Human, in the all-classes variants),")
            print("that is their detector saying 'this looks like a bot we trained on'.")

        return result

    except Exception as exc:
        if print_header:
            print("\n[REAL FP-AGENT VERDICT] could not run their full classifier in this environment:")
            print(" ", type(exc).__name__, str(exc)[:200])
            print("To get the *actual* fp-agent tool verdict:")
            print("  cd /path/to/fp-agent/classifier_training")
            print("  PROJECT_ROOT=$(pwd) uv run --with xgboost,scikit-learn,matplotlib,shap,orjson,psycopg2-binary,python-dotenv python /path/to/fp_agent_local_redteam_detector_test.py --real-verdict ...")
            print("Or run the full detector from the fp-agent root with the classifier_training src on PYTHONPATH and PROJECT_ROOT set to classifier_training/.")
            print("The raw frames JSON we saved can also be fed manually into their featurizer + load_model + predict.")
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="FP-Agent local redteam detector test")
    ap.add_argument("--port", type=int, default=34567)
    ap.add_argument("--interactions", type=int, default=2)
    ap.add_argument("--mode", choices=["evasion", "naive"], default="evasion")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip real browser/honey launch; exercise plan building, move_pointer import, and run the detector heuristics on synthetic 'evasion' vs 'naive' frame traces to validate the scoring logic quickly.",
    )
    ap.add_argument(
        "--real-verdict",
        action="store_true",
        help="After collecting (or in dry-run on synthetic), also run the *actual* fp-agent classifier (BehavioralFV + one of their pre-trained behavioral models) on the captured mus frames and print the real class prediction + confidence. Requires running with fp-agent classifier_training on PYTHONPATH and its deps (xgboost etc.) visible, and PROJECT_ROOT pointing at classifier_training/ so features/*.txt are found. If not possible, it will explain what is missing and fall back to the heuristic.",
    )
    ap.add_argument(
        "--visible",
        action="store_true",
        help="Launch browser in headed/visible mode so you can watch the interactions live on your desktop.",
    )
    args = ap.parse_args()

    if args.dry_run:
        print("=== FP-AGENT DETECTOR TEST (DRY RUN) ===")
        print("This exercises the 'setup' (behavior_plan + move_pointer from automation_connector) and the local detector heuristics without launching browsers or servers.")
        print("For the full recreation against real honey + mus.js + cloak humanize, run WITHOUT --dry-run (it will start the patched local honey and drive real styled interactions).")
        # Verify real imports / plan from tokyo
        try:
            plan = build_behavior_plan(
                {"id": "dry-detector", "automation_device": "desktop"},
                provider="fp_agent_detector_dry",
                seed="dry-evasion",
            )
            plan["pointer"]["move_style"] = "two_step"
            plan["pointer"]["imprecision_px"] = 5
            plan["pointer"]["overshoot_chance"] = 0.3
            print("  build_behavior_plan + pointer style override: OK")
            print("  sample plan.pointer:", plan.get("pointer"))
        except Exception as ex:
            print("  PLAN BUILD FAILED:", ex)
            sys.exit(2)

        # Synthetic frames: "naive" = straight-ish, regular timing, few reversals
        naive_mm = []
        t = 1000.0
        x, y = 100.0, 150.0
        for i in range(80):
            x += 2.8  # almost constant step
            y += 0.1
            t += 16.0  # very regular ~60fps
            naive_mm.append(["m", x, y, t])
        # a few clicks etc not needed for mm stats
        naive_curv = compute_straightness_and_curvature(naive_mm)
        naive_tcv = compute_timing_cv(naive_mm)
        naive_overs = count_overshoot_proxies(naive_mm)
        naive_score = bot_likeness_score(naive_curv, naive_tcv, naive_overs, len(naive_mm))
        print("\nNAIVE (synthetic rigid straight + regular timing):")
        print(f"  straightness={naive_curv['straightness']:.3f} curv_std={naive_curv['curv_angle_std']:.1f} tcv={naive_tcv:.2f} overs={naive_overs} -> bot_score={naive_score:.3f}")

        # "evasion" synthetic: wiggly from two_step style + wobble + bursty timing + overshoots
        ev_mm = []
        t = 1000.0
        x, y = 120.0, 160.0
        rng = random.Random(42)
        for i in range(80):
            # two_step like: main delta + small correction back/forth
            dx = rng.uniform(1.5, 4.5)
            dy = rng.uniform(-1.2, 1.8)
            x += dx
            y += dy
            t += rng.choice([8.0, 9.0, 12.0, 22.0, 31.0])  # bursty
            ev_mm.append(["m", x, y, t])
            if rng.random() < 0.35:  # micro correction (the "two_step" effect + wobble)
                x -= rng.uniform(0.8, 2.5)
                y -= rng.uniform(-0.6, 0.9)
                t += rng.uniform(4, 11)
                ev_mm.append(["m", x, y, t])
            if rng.random() < 0.18:  # occasional overshoot + back
                x += rng.uniform(6, 11)
                y += rng.uniform(-2, 3)
                t += 7
                ev_mm.append(["m", x, y, t])
                x -= rng.uniform(3, 7)  # correction
                y -= rng.uniform(-1, 2)
                t += rng.uniform(5, 14)
                ev_mm.append(["m", x, y, t])
        ev_curv = compute_straightness_and_curvature(ev_mm)
        ev_tcv = compute_timing_cv(ev_mm)
        ev_overs = count_overshoot_proxies(ev_mm)
        ev_score = bot_likeness_score(ev_curv, ev_tcv, ev_overs, len(ev_mm))
        print("\nEVASION (synthetic two_step corrections + wobble + bursty + overshoots):")
        print(f"  straightness={ev_curv['straightness']:.3f} curv_std={ev_curv['curv_angle_std']:.1f} tcv={ev_tcv:.2f} overs={ev_overs} -> bot_score={ev_score:.3f}")

        print("\n--- DETECTOR VERDICTS (synthetic, for logic validation) ---")
        print(f"NAIVE bot-likeness: {naive_score:.3f}  (expect > {BOT_LIKENESS_THRESHOLD} => would be picked up/clustered as automation)")
        print(f"EVASION bot-likeness: {ev_score:.3f} (expect < {BOT_LIKENESS_THRESHOLD} => evades, not clustered with rigid agents)")
        if ev_score < BOT_LIKENESS_THRESHOLD and naive_score >= BOT_LIKENESS_THRESHOLD:
            print("DRY-RUN SELF-CHECK: PASS (evasion synthetic scores lower / more human-like than naive)")
        else:
            print("DRY-RUN SELF-CHECK: scores may need threshold or heuristic tweak (still useful for iteration)")
        print("To run the REAL test (honey + cloak + mus capture + our actual move_pointer driving): omit --dry-run")
        return

    port = args.port
    ip = get_lan_ip()
    version = "redteamtest1"
    target = f"http://{ip}:{port}/{version}/"
    log_path = "/tmp/fp_agent_detector_honey.log"
    frames_path = f"/tmp/fp_redteam_frames_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    open(log_path, "w").close()

    proc = start_honey(port, log_path)
    try:
        ready_url = target.rstrip("/") + "/index.html"
        print("[DETECTOR] waiting for honey at", ready_url)
        if not wait_for_url(ready_url, timeout=args.timeout):
            print("[DETECTOR] WARNING: honey not responding on host yet; proceeding anyway")

        import tempfile
        video_dir = tempfile.mkdtemp(prefix="tokyo-redteam-movie-") if args.visible else None

        browser = launch_cloak(evasion=(args.mode == "evasion"), headless=not args.visible)
        ctx_kwargs = {
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "ignore_https_errors": True,
        }
        if args.visible and video_dir:
            ctx_kwargs["record_video_dir"] = video_dir
            ctx_kwargs["record_video_size"] = {"width": 1440, "height": 900}
            print(f"[DETECTOR] Recording movie (with red cursor + all human mechanics) to: {video_dir}")
            print("[DETECTOR] A .webm will be written when the session closes.")

        ctx = browser.new_context(**ctx_kwargs)
        ctx.set_default_timeout(15_000)
        page = ctx.new_page()

        if args.visible:
            enable_visible_mouse_tracking(page)
            print("[DETECTOR] >>> SYNTHETIC CURSOR ENABLED: large bright red 'HUMAN MOUSE' overlay is now active and will track every production move (arcs, two-step, positioned scrolls, hovers, peeks etc). Watch your desktop!")
            try:
                page.bring_to_front()
            except Exception:
                pass

            # Immediate calibration path: drive a few obvious mouse moves on the initial (blank) window
            # so you *definitely* see the red cursor graphic gliding before the real page navigation.
            print("[DETECTOR] >>> doing quick visible calibration mouse path on the launching window so the red HUMAN MOUSE is obvious right away...")
            for cx, cy in [(180, 160), (420, 190), (260, 320), (520, 240), (310, 290), (450, 310)]:
                page.mouse.move(cx, cy)
                page.wait_for_timeout(140)
            print("[DETECTOR] >>> calibration complete — red cursor moved visibly. Now running the real goal.")

        print("[DETECTOR] goto", target)
        page.goto(target, wait_until="domcontentloaded")
        time.sleep(0.6)

        # plan for our real primitives (only used in evasion; naive ignores it)
        plan = build_behavior_plan(
            {"id": "detector-test", "automation_device": "desktop"},
            provider="fp_agent_detector",
            seed=f"detector-{args.mode}",
        )
        if args.mode == "evasion":
            plan["pointer"]["move_style"] = "two_step"
            plan["pointer"]["imprecision_px"] = 4
            plan["pointer"]["overshoot_chance"] = 0.28
        recorder = SessionRecorder(plan)

        all_captured_frames: list = []

        def drive_and_capture(label: str):
            nonlocal all_captured_frames
            ensure_mus_recording(page)
            time.sleep(0.15)  # let mus init and start capturing events
            if args.mode == "evasion":
                for k in range(2):
                    x = 200 + random.randint(-30, 180) + (k * 120)
                    y = 160 + random.randint(-20, 80)
                    move_pointer(page, x, y, plan, recorder=recorder)
                    scroll_page(page, plan, k, 280, recorder=recorder)
                    try:
                        click_element(page, "a, button, .post, input", plan, recorder=recorder)
                    except Exception:
                        pass
                    wait_human(page, plan, k, 220, recorder=recorder)
            else:
                # naive direct
                for k in range(8):
                    page.mouse.move(180 + k * 35, 150 + (k % 3) * 10)
                    time.sleep(0.03)

            time.sleep(0.7)
            frames = stop_and_get_mus_frames(page)
            mm_count = sum(1 for f in frames if f and f[0] == "m")
            print(f"[DETECTOR] {label}: captured {len(frames)} mus frames ({mm_count} mouse moves)")
            all_captured_frames.extend(frames)

            # force the collector to see exactly these frames
            force_post_mus_frames(page, ip, port, version, frames)

            # visit a couple more pages for richer context (each gets its own recording window)
            for sub in ["forums", "shop"]:
                try:
                    page.goto(f"http://{ip}:{port}/{version}/{sub}", wait_until="domcontentloaded")
                    time.sleep(0.4)
                    ensure_mus_recording(page)
                    time.sleep(0.15)
                    if args.mode == "evasion":
                        move_pointer(page, 300, 220, plan, recorder=recorder)
                        scroll_page(page, plan, 0, 350, recorder=recorder)
                    else:
                        for kk in range(5):
                            page.mouse.move(250 + kk * 40, 200)
                            time.sleep(0.02)
                    time.sleep(0.5)
                    more = stop_and_get_mus_frames(page)
                    all_captured_frames.extend(more)
                    force_post_mus_frames(page, ip, port, version, more)
                except Exception as ex:
                    print("[DETECTOR] subpage skip", sub, ex)

        drive_and_capture("main interactions")

        # Save raw for offline analysis with their featurizer
        with open(frames_path, "w") as f:
            json.dump({"mode": args.mode, "frames": all_captured_frames}, f)
        print(f"[DETECTOR] raw frames saved to {frames_path}")

        # --- Run the detector ---
        mm_only = [f for f in all_captured_frames if f and f[0] == "m"]
        n_moves = len(mm_only)
        curv = compute_straightness_and_curvature(mm_only)
        t_cv = compute_timing_cv(mm_only)
        overs = count_overshoot_proxies(mm_only)
        score = bot_likeness_score(curv, t_cv, overs, n_moves)

        print("\n=== FP-AGENT LOCAL REDTEAM DETECTOR RESULTS ===")
        print(f"Mode: {args.mode}")
        print(f"Total mus frames: {len(all_captured_frames)} (mouse moves: {n_moves})")
        print(f"Straightness (1.0=rigid straight): {curv['straightness']:.3f}")
        print(f"Curvature angle std (deg): {curv['curv_angle_std']:.1f}")
        print(f"Curvature dist mean: {curv['curv_dist_mean']:.2f}")
        print(f"Timing CV (higher=bursty/variable): {t_cv:.2f}")
        print(f"Overshoot+corr proxies: {overs}")
        print(f"Composite bot-likeness score: {score:.3f} (threshold {BOT_LIKENESS_THRESHOLD} for 'likely known automation cluster')")

        if score < BOT_LIKENESS_THRESHOLD:
            verdict = "EVADES (low bot-likeness). Our humanize + styled moves produced varied, curved, bursty traces unlikely to cluster with the rigid agents in fp-agent's training data."
        else:
            verdict = "DETECTED / clusters as bot-like. Score above threshold; would likely be picked up by their current classifier."

        print(f"VERDICT: {verdict}")
        print("Check the honey log for the exact frames that were POSTed to /mouse_movement (the behavioral data the detector would see).")
        print(f"  tail -200 {log_path} | grep -E 'mouse_movement|frames|POST'")

        # Also print a couple sample frames for quick inspection
        print("Sample mouse frames (first 5):")
        for f in mm_only[:5]:
            print("  ", f)

        # Real tool verdict if requested (or always attempt in a way that is silent on failure)
        if args.real_verdict or args.dry_run:
            real = try_real_fp_agent_verdict(all_captured_frames, print_header=True)
            if real:
                print(f"\n[INTEGRATED] fp-agent real verdict available: {real['predicted_label']} (conf {real.get('confidence')})")

        if args.visible:
            print("\n[DETECTOR] >>> BROWSER WINDOW STILL OPEN — the large red 'human mouse' cursor is parked on the page.")
            print("[DETECTOR] >>> Look at your desktop now: watch the cursor graphic, the page content, any hovers left active.")
            print("[DETECTOR] >>> You can manually move/click in the window too if you want. Close the browser tab/window when done staring at the mechanics.")
            print("[DETECTOR] >>> (This window will auto-close in ~8 seconds unless you close it first.)")
            time.sleep(8)

        # Close context and browser to finalize the movie file
        try:
            ctx.close()
            browser.close()
        except Exception:
            pass

        # Copy the movie if recorded
        if args.visible and video_dir and os.path.isdir(video_dir):
            try:
                import shutil
                from pathlib import Path
                home = Path.home()
                desktop = home / "Desktop"
                desktop.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                
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
                    stamped = desktop / f"tokyo-watch-movie-redteam-{ts}.webm"
                    latest_desk = desktop / "tokyo-latest-watch-movie.webm"
                    codex_dir = Path(TOKYO_WT) / ".codex-outputs"
                    codex_dir.mkdir(parents=True, exist_ok=True)
                    stamped_codex = codex_dir / f"watch-redteam-{ts}.webm"
                    latest_codex = codex_dir / "latest-watch-movie.webm"
                    
                    shutil.copy2(src, stamped)
                    shutil.copy2(src, latest_desk)
                    shutil.copy2(src, stamped_codex)
                    shutil.copy2(src, latest_codex)
                    
                    print("\n" + "=" * 70)
                    print("[DETECTOR] *** MOVIE RECORDED (the reliable way to see the mouse) ***")
                    print(f"  Saved timestamped copy to Desktop: {stamped}")
                    print(f"  Saved rolling copy to Desktop: {latest_desk}")
                    print(f"  Saved timestamped copy to .codex-outputs: {stamped_codex}")
                    print(f"  Saved rolling copy to .codex-outputs: {latest_codex}")
                    print("=" * 70 + "\n")
            except Exception as copy_err:
                print(f"[DETECTOR] could not copy Watch movie: {copy_err}")

    finally:
        try:
            proc.terminate()
            time.sleep(0.5)
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    main()
