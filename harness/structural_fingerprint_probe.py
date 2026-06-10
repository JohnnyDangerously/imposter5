"""Structural fingerprint probe — the *cheap, non-kinematic* tell surface.

Kinematic realism (mouse curves, dwell, Markov) is worthless if a detector can
catch us in one synchronous property read: ``navigator.webdriver === true``, a
``HeadlessChrome`` UA, a monkey-patched ``toString``, a worker/iframe prototype
leak, a SwiftShader GPU, or synthetic mouse events with ``movementX === 0``.

This probe launches the **exact** gauntlet runtime (CloakBrowserRunner + the
stealth context kwargs + the anti-fingerprint init script) and reports, in one
table:

  1. Blue's own Layer 2 battery (verbatim from gauntlet.html runLayer2Checks) —
     the surface that *gates* the HUMAN_EVADED verdict (webdriver + iframe are
     critical hard-fails; monkey-patch / headless / GPU / worker are penalties).
  2. The broader real-detector surface (CreepJS / FingerprintJS / DataDome style)
     that Blue does NOT check, so passing Blue doesn't lull us: automation
     globals, chrome object, permissions/Notification coherence, plugins/mime
     realism, languages, platform-vs-UA, client hints, WebGL vendor.
  3. Event-level structural coherence of our synthetic motor output: isTrusted,
     movementX/Y, and whether page.mouse fires pointer events alongside mouse
     events (a real mouse fires both).

Usage:
  PYTHONPATH=src python harness/structural_fingerprint_probe.py [--url URL] [--headed]

Default URL is the live Blue gauntlet page so the probe runs on a real http
origin (some patches only fully apply off about:blank).
"""
from __future__ import annotations

import argparse
import json
import sys


JS_BATTERY = r"""
() => {
  const out = {};

  // ---- Blue Layer 2: monkey-patch toString integrity ----
  const fns = {
    "Function.prototype.toString": Function.prototype.toString,
    "navigator.plugins": Object.getOwnPropertyDescriptor(Navigator.prototype, "plugins")?.get,
    "navigator.languages": Object.getOwnPropertyDescriptor(Navigator.prototype, "languages")?.get,
    "WebGLRenderingContext.prototype.getParameter": WebGLRenderingContext.prototype.getParameter,
  };
  out.monkey_patches = {};
  for (const [name, fn] of Object.entries(fns)) {
    if (fn) {
      try {
        const s = fn.toString();
        out.monkey_patches[name] = (!s.includes("[native code]") || s.length > 150);
      } catch { out.monkey_patches[name] = true; }
    } else {
      out.monkey_patches[name] = "absent";
    }
  }

  // ---- Blue Layer 2: automation indicators ----
  out.webdriver = navigator.webdriver === true;
  out.headless = /HeadlessChrome/.test(navigator.userAgent) ||
                 (window.outerWidth === 0 && window.outerHeight === 0);

  // ---- Blue Layer 2: iframe prototype leak ----
  try {
    const iframe = document.createElement("iframe");
    iframe.style.display = "none";
    document.body.appendChild(iframe);
    const iw = iframe.contentWindow;
    const mp = navigator.plugins.length, ip = iw.navigator.plugins.length;
    const md = Object.getOwnPropertyDescriptor(Navigator.prototype, "webdriver")?.get?.toString() || "";
    const idd = Object.getOwnPropertyDescriptor(iw.Navigator.prototype, "webdriver")?.get?.toString() || "";
    out.iframe_leak = (mp !== ip) || (md !== idd && md !== "");
    out.iframe_detail = { main_plugins: mp, iframe_plugins: ip, main_wd_tostr: md, iframe_wd_tostr: idd };
    document.body.removeChild(iframe);
  } catch (e) { out.iframe_leak = true; out.iframe_detail = String(e); }

  // ---- Blue Layer 2: WebGL renderer ----
  try {
    const c = document.createElement("canvas");
    const gl = c.getContext("webgl") || c.getContext("experimental-webgl");
    if (!gl) { out.webgl_renderer = ""; out.webgl_vendor = ""; }
    else {
      const dbg = gl.getExtension("WEBGL_debug_renderer_info");
      out.webgl_renderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : "";
      out.webgl_vendor = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : "";
    }
  } catch (e) { out.webgl_renderer = "error"; out.webgl_vendor = "error"; }

  // ---- Broader real-detector surface (NOT checked by Blue) ----
  const autoGlobals = [
    "cdc_adoQpoasnfa76pfcZLmcfl_Array", "cdc_adoQpoasnfa76pfcZLmcfl_Promise",
    "__nightmare", "__phantomas", "_phantom", "callPhantom", "callSelenium",
    "_selenium", "__selenium_evaluate", "__webdriver_evaluate", "__driver_evaluate",
    "__webdriver_script_fn", "__fxdriver_evaluate", "__driver_unwrapped",
    "domAutomation", "domAutomationController", "__playwright", "__pw_manual",
  ];
  out.automation_globals = autoGlobals.filter((k) => k in window);
  // any window key that looks like a chromedriver cdc_ artifact
  out.cdc_keys = Object.keys(window).filter((k) => /^cdc_|^\$cdc_/.test(k));

  out.has_chrome = !!window.chrome;
  out.has_chrome_runtime = !!(window.chrome && window.chrome.runtime);
  out.ua = navigator.userAgent;
  out.platform = navigator.platform;
  out.languages = navigator.languages;
  out.plugins_len = navigator.plugins.length;
  out.mime_len = navigator.mimeTypes.length;
  out.hardwareConcurrency = navigator.hardwareConcurrency;
  out.deviceMemory = navigator.deviceMemory;
  out.outerWidth = window.outerWidth;
  out.outerHeight = window.outerHeight;
  out.screen = { w: screen.width, h: screen.height, availW: screen.availWidth, availH: screen.availHeight };
  out.devicePixelRatio = window.devicePixelRatio;

  // Client Hints (modern desktop Chrome populates this; absence on a Chrome UA is a tell)
  try {
    out.uadata_brands = navigator.userAgentData ? navigator.userAgentData.brands.map((b) => b.brand) : null;
    out.uadata_mobile = navigator.userAgentData ? navigator.userAgentData.mobile : null;
  } catch (e) { out.uadata_brands = "error"; }

  // platform vs UA coherence
  const uaMac = /Mac OS X/.test(navigator.userAgent);
  out.platform_ua_mismatch = uaMac ? (navigator.platform !== "MacIntel") : false;

  out.has_chrome_runtime = !!(window.chrome && window.chrome.runtime);

  // ---- Deep "lie-detector" vectors (CreepJS / FingerprintJS class) ----
  // A native function has NO own 'prototype' property, name matches, length is
  // canonical, and toString reports [native code]. A naive JS override of
  // toString/getters that *spoofs the string* still leaks here.
  const lie = {};
  const ts = Function.prototype.toString;
  lie.toString_has_prototype = ("prototype" in ts);          // native: false
  lie.toString_name = ts.name;                                // native: "toString"
  lie.toString_length = ts.length;                            // native: 0
  // applying the (possibly patched) toString to itself must still say native
  try { lie.toString_self = /\[native code\]/.test(ts.call(ts)); } catch (e) { lie.toString_self = "err"; }
  // the webdriver getter: a real absent/native getter has no own prototype
  const wdGet = Object.getOwnPropertyDescriptor(Navigator.prototype, "webdriver")?.get;
  lie.webdriver_getter_has_prototype = wdGet ? ("prototype" in wdGet) : "absent";
  lie.webdriver_getter_name = wdGet ? wdGet.name : "absent";
  // toExposed: does redefining throw a TypeError consistent with native data props?
  out.lie = lie;

  return out;
}
"""

# Permissions/Notification coherence has to await a promise, so it's a separate eval.
JS_PERMISSIONS = r"""
async () => {
  const out = {};
  out.notification_permission = (typeof Notification !== "undefined") ? Notification.permission : "no-Notification";
  try {
    const st = await navigator.permissions.query({ name: "notifications" });
    out.permissions_state = st.state;
    // Classic headless tell: Notification.permission === 'denied' while
    // permissions.query reports 'prompt' (or vice versa). A real browser is consistent.
    out.permission_mismatch = (out.notification_permission === "denied" && st.state === "prompt") ||
                              (out.notification_permission === "default" && st.state === "denied");
  } catch (e) { out.permissions_state = "error:" + e; out.permission_mismatch = false; }
  return out;
}
"""

# Event-level coherence: capture the next mouse/pointer events the motor model emits.
JS_INSTALL_EVENT_CAPTURE = r"""
() => {
  window.__probe_events = [];
  const rec = (kind) => (e) => {
    if (window.__probe_events.length > 200) return;
    window.__probe_events.push({
      kind, type: e.type, isTrusted: e.isTrusted,
      movementX: e.movementX, movementY: e.movementY,
      clientX: e.clientX, clientY: e.clientY,
      screenX: e.screenX, screenY: e.screenY,
      buttons: e.buttons, pointerType: e.pointerType,
      deltaY: e.deltaY,
    });
  };
  ["mousemove", "mousedown", "mouseup", "click"].forEach((t) =>
    window.addEventListener(t, rec("mouse"), true));
  ["pointermove", "pointerdown", "pointerup"].forEach((t) =>
    window.addEventListener(t, rec("pointer"), true));
  window.addEventListener("wheel", rec("wheel"), true);
}
"""

JS_READ_EVENTS = "() => window.__probe_events || []"


def _launch():
    from imposter5.automation_connector.browser_runner import get_browser_runner
    from imposter5.loaders.cloak_runtime import (
        apply_anti_fingerprint_init_script,
        automation_connector_stealth_context_kwargs,
    )

    runner = get_browser_runner()
    return runner, apply_anti_fingerprint_init_script, automation_connector_stealth_context_kwargs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5190/gauntlet")
    ap.add_argument("--headed", action="store_true", help="launch headed (default headless, matching gauntlet)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON only")
    args = ap.parse_args()

    runner, apply_anti_fp, stealth_kwargs = _launch()
    headless = not args.headed

    browser = runner.launch_browser(headless=headless)
    ctx_kwargs = stealth_kwargs()
    context = browser.new_context(**ctx_kwargs)
    try:
        apply_anti_fp(context)
    except Exception:
        pass
    context.set_default_timeout(20_000)
    page = context.new_page()

    try:
        page.goto(args.url, wait_until="domcontentloaded")
    except Exception as e:
        print(f"[probe] navigation to {args.url} failed: {e}; falling back to about:blank-ish data page")
        page.set_content("<html><body><p id=t>structural probe target. lorem ipsum dolor sit amet "
                         "consectetur adipiscing elit sed do eiusmod tempor.</p></body></html>")

    page.wait_for_timeout(400)

    env = page.evaluate(JS_BATTERY)
    try:
        perms = page.evaluate(JS_PERMISSIONS)
    except Exception as e:
        perms = {"permissions_state": f"error:{e}", "permission_mismatch": False}
    env.update(perms)

    # Event coherence: install capture, drive the real motor primitives, read back.
    page.evaluate(JS_INSTALL_EVENT_CAPTURE)
    try:
        page.mouse.move(300, 300)
        page.mouse.move(520, 410, steps=8)
        page.mouse.move(640, 300, steps=6)
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(150)
    except Exception as e:
        print(f"[probe] motor drive failed: {e}")
    events = page.evaluate(JS_READ_EVENTS)

    try:
        context.close()
    except Exception:
        pass
    try:
        browser.close()
    except Exception:
        pass

    # ---- analysis ----
    mouse_moves = [e for e in events if e.get("type") == "mousemove"]
    pointer_moves = [e for e in events if e.get("type") == "pointermove"]
    wheels = [e for e in events if e.get("type") == "wheel"]
    untrusted = [e for e in events if e.get("isTrusted") is False]
    nonzero_movement = [e for e in mouse_moves if (e.get("movementX") or 0) != 0 or (e.get("movementY") or 0) != 0]

    event_findings = {
        "mousemove_count": len(mouse_moves),
        "pointermove_count": len(pointer_moves),
        "wheel_count": len(wheels),
        "any_untrusted": len(untrusted) > 0,
        "movementXY_all_zero": len(mouse_moves) > 0 and len(nonzero_movement) == 0,
        "pointer_events_present": len(pointer_moves) > 0,
    }

    if args.json:
        print(json.dumps({"env": env, "events": event_findings, "raw_events": events[:20]}, indent=2, default=str))
        return 0

    # ---- pretty report with PASS / LEAK verdicts ----
    def line(label, leak, detail=""):
        tag = "LEAK" if leak else "ok  "
        bang = "  <<<" if leak else ""
        print(f"  [{tag}] {label:<46} {detail}{bang}")

    leaks_critical = []
    leaks_high = []
    leaks_soft = []

    print("\n" + "=" * 78)
    print(f"  STRUCTURAL FINGERPRINT PROBE  —  {args.url}  (headless={headless})")
    print("=" * 78)

    print("\n-- Blue Layer 2 (gates the verdict) --")
    line("navigator.webdriver === true [CRITICAL]", env["webdriver"], str(env["webdriver"]))
    if env["webdriver"]:
        leaks_critical.append("navigator.webdriver")
    line("iframe prototype leak [CRITICAL]", env["iframe_leak"], json.dumps(env.get("iframe_detail"))[:60])
    if env["iframe_leak"]:
        leaks_critical.append("iframe_prototype_leak")
    line("headless indicator (UA / 0-window) [HIGH]", env["headless"],
         f'outer={env["outerWidth"]}x{env["outerHeight"]}')
    if env["headless"]:
        leaks_high.append("headless")
    for name, status in env["monkey_patches"].items():
        is_leak = status is True
        line(f"monkey-patch toString: {name} [HIGH]", is_leak, str(status))
        if is_leak:
            leaks_high.append(f"monkey_patch:{name}")
    gpu = str(env.get("webgl_renderer", "")).lower()
    gpu_leak = any(k in gpu for k in ("swiftshader", "llvmpipe", "vmware", "virtualbox", "software"))
    line("WebGL renderer (software GPU) [HIGH]", gpu_leak or not gpu,
         f'{env.get("webgl_renderer")!r} / {env.get("webgl_vendor")!r}')
    if gpu_leak:
        leaks_high.append("software_gpu")
    elif not gpu:
        leaks_soft.append("empty_webgl")

    print("\n-- Broader real-detector surface (Blue does NOT check) --")
    line("automation globals present", bool(env["automation_globals"]), str(env["automation_globals"]))
    if env["automation_globals"]:
        leaks_high.append("automation_globals")
    line("cdc_ chromedriver keys", bool(env["cdc_keys"]), str(env["cdc_keys"]))
    if env["cdc_keys"]:
        leaks_high.append("cdc_keys")
    line("window.chrome present (expected on Chrome UA)", not env["has_chrome"], str(env["has_chrome"]))
    if not env["has_chrome"]:
        leaks_soft.append("no_window_chrome")
    line("navigator.plugins empty (0 is suspicious)", env["plugins_len"] == 0, f'{env["plugins_len"]} plugins')
    if env["plugins_len"] == 0:
        leaks_soft.append("empty_plugins")
    line("navigator.languages empty", not env["languages"], str(env["languages"]))
    if not env["languages"]:
        leaks_high.append("empty_languages")
    line("platform vs UA mismatch", env["platform_ua_mismatch"],
         f'{env["platform"]!r} vs UA')
    if env["platform_ua_mismatch"]:
        leaks_high.append("platform_ua_mismatch")
    line("hardwareConcurrency missing/zero", not env.get("hardwareConcurrency"),
         str(env.get("hardwareConcurrency")))
    if not env.get("hardwareConcurrency"):
        leaks_soft.append("no_hardwareConcurrency")
    line("permissions/Notification mismatch", env.get("permission_mismatch"),
         f'{env.get("notification_permission")} / {env.get("permissions_state")}')
    if env.get("permission_mismatch"):
        leaks_high.append("permission_mismatch")
    line("client-hints brands missing (on Chrome UA)", env.get("uadata_brands") in (None, "error"),
         str(env.get("uadata_brands")))
    if env.get("uadata_brands") in (None, "error"):
        leaks_soft.append("no_client_hints")

    print("\n-- Deep lie-detector vectors (CreepJS / FingerprintJS class; Blue does NOT check) --")
    lie = env.get("lie", {}) or {}
    line("toString has own .prototype (native has none)", lie.get("toString_has_prototype") is True,
         str(lie.get("toString_has_prototype")))
    if lie.get("toString_has_prototype") is True:
        leaks_high.append("toString_prototype_leak")
    line("toString.name != 'toString'", lie.get("toString_name") != "toString",
         str(lie.get("toString_name")))
    if lie.get("toString_name") != "toString":
        leaks_high.append("toString_name")
    line("toString.length != 0", lie.get("toString_length") not in (0,),
         str(lie.get("toString_length")))
    if lie.get("toString_length") not in (0,):
        leaks_soft.append("toString_length")
    line("toString(toString) not native", lie.get("toString_self") is not True,
         str(lie.get("toString_self")))
    if lie.get("toString_self") is not True:
        leaks_high.append("toString_self_not_native")
    wdp = lie.get("webdriver_getter_has_prototype")
    line("webdriver getter has own .prototype", wdp is True, str(wdp))
    if wdp is True:
        leaks_high.append("webdriver_getter_prototype_leak")
    # NOTE: chrome.runtime absence is NOT a tell on modern Chrome. Verified
    # against a real headful Chrome (window.chrome present, chrome.runtime
    # undefined, Object.keys(chrome)===[]) — Chrome restricted runtime from
    # regular web pages years ago. Faking it diverges from the real baseline
    # and is *more* detectable, so we only note it, never flag it.
    line("chrome.runtime present (informational; absent==real Chrome)", False,
         f'{env.get("has_chrome_runtime")} (real Chrome baseline: absent)')

    print("\n-- Event-level coherence of our synthetic motor output --")
    line("any synthetic event isTrusted===false", event_findings["any_untrusted"],
         f'{event_findings["mousemove_count"]} moves observed')
    if event_findings["any_untrusted"]:
        leaks_critical.append("untrusted_events")
    line("movementX/Y all zero on mousemove", event_findings["movementXY_all_zero"],
         "real mice have nonzero movement deltas")
    if event_findings["movementXY_all_zero"]:
        leaks_high.append("movementXY_zero")
    line("pointer events absent (mouse w/o pointer)", not event_findings["pointer_events_present"],
         f'{event_findings["pointermove_count"]} pointermove')
    if not event_findings["pointer_events_present"]:
        leaks_high.append("no_pointer_events")

    print("\n" + "=" * 78)
    print(f"  CRITICAL leaks (instant BOT_DETECTED): {leaks_critical or 'none'}")
    print(f"  HIGH leaks     (penalty / real-detector): {leaks_high or 'none'}")
    print(f"  SOFT signals   (weak / context-dependent): {leaks_soft or 'none'}")
    print("=" * 78 + "\n")

    return 2 if leaks_critical else (1 if leaks_high else 0)


if __name__ == "__main__":
    sys.exit(main())
