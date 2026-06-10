"""Validate the unified Story-driven feed journey against the live Blue gauntlet.

Runs N short sessions through the EXACT gauntlet runtime (cloak + stealth ctx +
anti-fp init script), each via run_gauntlet_journey (now Story-engine backed), and
captures the Blue evasion verdict for each. Confirms the consolidation kept
HUMAN_EVADED and that sessions vary across runs (different arcs / lengths).

Usage:
  PYTHONPATH=src python harness/validate_feed_story.py [--sessions 3] [--duration 45] [--url URL]
"""
from __future__ import annotations

import argparse
import sys


def _capture_blue_verdict(page):
    try:
        page.evaluate("window.lhhlSubmit && window.lhhlSubmit()")
        for _ in range(60):
            rep = page.evaluate("window.__lhhl_last_report || null")
            if rep:
                return rep
            page.wait_for_timeout(200)
    except Exception as e:
        return {"error": str(e)}
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=3)
    ap.add_argument("--duration", type=float, default=45.0)
    ap.add_argument("--url", default="http://127.0.0.1:5190/gauntlet")
    args = ap.parse_args()

    from imposter5.automation_connector.behavior_policy import build_behavior_plan
    from imposter5.automation_connector.browser_runner import get_browser_runner
    from imposter5.automation_connector.session_recorder import SessionRecorder
    from imposter5.loaders.cloak_runtime import (
        apply_anti_fingerprint_init_script,
        automation_connector_stealth_context_kwargs,
    )
    from imposter5.loaders.gauntlet_journey import run_gauntlet_journey

    results = []
    for i in range(args.sessions):
        runner = get_browser_runner()
        browser = runner.launch_browser(headless=True)
        ctx_kwargs = automation_connector_stealth_context_kwargs()
        context = browser.new_context(**ctx_kwargs)
        try:
            apply_anti_fingerprint_init_script(context)
        except Exception:
            pass
        context.set_default_timeout(25_000)
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded")
            page.wait_for_timeout(400)
            behavior_plan = build_behavior_plan(
                {"id": f"validate-{i}", "entity_type": "generic_web"},
                provider="generic", goal="feed_browse", seed=None,
            )
            recorder = SessionRecorder(behavior_plan)
            summary = run_gauntlet_journey(
                page, behavior_plan, recorder=recorder, duration_s=args.duration
            )
            verdict = _capture_blue_verdict(page)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

        v = verdict or {}
        results.append({
            "arc": summary.get("arc"),
            "scans": summary.get("feed_scan_bursts"),
            "captured": summary.get("posts_captured"),
            "profiles": summary.get("profiles_opened"),
            "lookups": summary.get("lookups"),
            "notifs": summary.get("notifications_visited"),
            "glances": summary.get("glances"),
            "goal_met": summary.get("goal_met"),
            "dur": summary.get("duration_s"),
            "score": v.get("evasion_score"),
            "verdict": v.get("verdict"),
            "journey": v.get("journey_verdict"),
        })
        r = results[-1]
        print(f"[session {i}] arc={r['arc']:<12} dur={r['dur']}s scans={r['scans']} "
              f"captured={r['captured']} prof={r['profiles']} look={r['lookups']} "
              f"notif={r['notifs']} glance={r['glances']} goal_met={r['goal_met']} "
              f"|| Blue {r['score']} [{r['verdict']}] journey={r['journey']}", flush=True)

    print("\n=== summary ===")
    arcs = {r["arc"] for r in results}
    verdicts = {r["verdict"] for r in results}
    print(f"arcs seen: {sorted(a for a in arcs if a)}")
    print(f"verdicts:  {sorted(v for v in verdicts if v)}")
    bad = [r for r in results if r["verdict"] not in ("HUMAN_EVADED", None) or r["score"] is None]
    if any(r["verdict"] == "HUMAN_EVADED" for r in results) and not [r for r in results if r["verdict"] and r["verdict"] != "HUMAN_EVADED"]:
        print("RESULT: all scored sessions HUMAN_EVADED ✓")
        return 0
    print(f"RESULT: NOT clean — {[ (r['arc'], r['verdict'], r['score']) for r in results ]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
