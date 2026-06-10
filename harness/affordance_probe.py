"""Probe a site's Red Team Automation profile resolution — live.

Navigates a URL through the real gauntlet runtime (cloak + stealth ctx + anti-fp
init script), resolves every affordance role via the cascade, and prints which
strategy won (profile-css / semantic-css / profile-text / default-text / field-css)
plus the winning selector and match count. Use it once per new site to confirm the
profile resolves ~9/10 roles with no per-run fiddling before launching a campaign.

Usage:
  PYTHONPATH=src python harness/affordance_probe.py --url http://127.0.0.1:5190/gauntlet
  PYTHONPATH=src python harness/affordance_probe.py --url https://www.linkedin.com/feed \
      --profile linkedin --cookies-user 51197947
  PYTHONPATH=src python harness/affordance_probe.py --url <url> --profile /path/to/profile.json

For authed sites (LinkedIn) supply stored cookies via --cookies-user or run after a
login session so the feed actually renders; otherwise feed roles read the logged-out
page. Critical clickable roles are weighted; the exit code is non-zero if any of them
fail to resolve.
"""
from __future__ import annotations

import argparse
import json
import sys

# Roles whose failure means a feed campaign can't run on this site.
CRITICAL = {"feed_post", "nav_home", "nav_notifications", "search_input"}


def _load_profile(arg: str | None, url: str):
    from imposter5.automation_connector import affordance as aff

    if not arg or arg == "auto":
        return aff.builtin_profile_for_url(url)
    if arg in ("linkedin", "gauntlet"):
        return aff._BUILTIN_BY_NAME[arg]
    with open(arg) as f:
        return aff.AutomationProfile.from_dict(json.load(f))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--profile", default="auto", help="auto | linkedin | gauntlet | <path-to-profile.json>")
    ap.add_argument("--cookies-user", default=None, help="entity_int_id whose stored cookies to load (authed sites)")
    ap.add_argument("--settle", type=float, default=2.0, help="seconds to wait after load for the feed to render")
    ap.add_argument("--headless", action="store_true", default=True)
    args = ap.parse_args()

    from imposter5.automation_connector.affordance import CLICKABLE_ROLES, FIELD_ROLES, RoleResolver
    from imposter5.automation_connector.browser_runner import get_browser_runner
    from imposter5.loaders.cloak_runtime import (
        apply_anti_fingerprint_init_script,
        automation_connector_stealth_context_kwargs,
    )

    profile = _load_profile(args.profile, args.url)
    print(f"profile: {profile.name}  url: {args.url}\n")

    runner = get_browser_runner()
    browser = runner.launch_browser(headless=args.headless)
    ctx_kwargs = automation_connector_stealth_context_kwargs()
    context = browser.new_context(**ctx_kwargs)
    try:
        apply_anti_fingerprint_init_script(context)
    except Exception:
        pass
    if args.cookies_user:
        try:
            from imposter5.automation_connector.login_manager import load_site_cookies

            cookies = load_site_cookies(args.cookies_user, args.url)
            if cookies:
                context.add_cookies(cookies)
                print(f"(loaded {len(cookies)} stored cookies for user {args.cookies_user})\n")
        except Exception as e:
            print(f"(cookie load failed: {e})\n")
    context.set_default_timeout(25_000)
    page = context.new_page()

    rows: list[dict] = []
    try:
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(int(args.settle * 1000))
        resolver = RoleResolver(page, profile)
        for role in CLICKABLE_ROLES:
            rows.append(resolver.explain(role))
        for role in FIELD_ROLES:
            rows.append(resolver.explain_field(role))
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

    ok = sum(1 for r in rows if r["ok"])
    crit_fail = [r["role"] for r in rows if r["role"] in CRITICAL and not r["ok"]]
    print(f"{'role':<20} {'ok':<4} {'strategy':<14} {'count':<6} match")
    print("-" * 78)
    for r in rows:
        mark = "ok" if r["ok"] else "MISS"
        crit = " *" if r["role"] in CRITICAL else "  "
        print(f"{r['role']:<20}{crit}{mark:<4} {str(r['strategy'] or ''):<14} {r['count']:<6} {r['match'] or ''}")
    print("-" * 78)
    print(f"resolved {ok}/{len(rows)} roles  (* = critical)")
    if crit_fail:
        print(f"CRITICAL roles unresolved: {crit_fail}")
        return 1
    print("all critical roles resolved \u2713")
    return 0


if __name__ == "__main__":
    sys.exit(main())
