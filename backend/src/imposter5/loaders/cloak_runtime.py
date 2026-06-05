"""Shared CloakBrowser runtime options + the single launch surface for automation connector sessions.

This is the *only* place that should import from the cloakbrowser package for the connector
(launch / launch_persistent_context). Centralization makes the Cloak integration non-brittle
for package upgrades and lets redteam tuning (via AUTOMATION_CONNECTOR_HUMAN_CONFIG etc)
affect all paths (campaigns, agent prompt actions, LinkedIn static, login sessions) uniformly.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

SUPPORTED_HUMAN_PRESETS = {"default", "careful"}
DEFAULT_HUMAN_PRESET = "careful"
DEFAULT_LOCALE = "en-US"
DEFAULT_TIMEZONE = "America/New_York"


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def automation_connector_humanize_enabled() -> bool:
    """Whether Cloak humanize layer (and styled mouse moves) are active for automation runs."""
    return _env_bool("AUTOMATION_CONNECTOR_HUMANIZE", default=True)


def automation_connector_locale() -> str:
    """Return the locale used by Cloak-backed automation sessions."""
    return os.environ.get("AUTOMATION_CONNECTOR_LOCALE", DEFAULT_LOCALE)


def automation_connector_timezone() -> str:
    """Return the timezone used by Cloak-backed automation sessions."""
    return os.environ.get("AUTOMATION_CONNECTOR_TIMEZONE", DEFAULT_TIMEZONE)


def automation_connector_human_preset() -> str:
    """Return the configured Cloak human preset, constrained to known presets."""
    preset = os.environ.get("AUTOMATION_CONNECTOR_HUMAN_PRESET", DEFAULT_HUMAN_PRESET).strip()
    if preset in SUPPORTED_HUMAN_PRESETS:
        return preset
    logger.warning(
        "[automation_connector] unsupported Cloak human preset %r; using %s",
        preset,
        DEFAULT_HUMAN_PRESET,
    )
    return DEFAULT_HUMAN_PRESET


def automation_connector_human_config() -> dict[str, Any] | None:
    """Optional overrides for Cloak HumanConfig (mouse/typing/scroll timings) from JSON env.

    Allows red-team tuning of movement expressiveness without changing presets, e.g.
    higher wobble or step counts to increase trajectory variety similar to Camoufox knot distortion.
    """
    raw = os.environ.get("AUTOMATION_CONNECTOR_HUMAN_CONFIG")
    if not raw:
        return None
    try:
        val = json.loads(raw)
        if isinstance(val, dict):
            return val
    except Exception:
        logger.warning("[automation_connector] invalid AUTOMATION_CONNECTOR_HUMAN_CONFIG JSON; ignoring")
    return None


def automation_connector_cloak_options() -> dict[str, Any]:
    """Return CloakBrowser launch kwargs shared across connector browser paths."""
    options: dict[str, Any] = {
        "humanize": automation_connector_humanize_enabled(),
        "human_preset": automation_connector_human_preset(),
    }

    hc = automation_connector_human_config()
    if hc:
        options["human_config"] = hc

    proxy = os.environ.get("AUTOMATION_CONNECTOR_PROXY")
    if proxy:
        options["proxy"] = proxy

    if _env_bool("AUTOMATION_CONNECTOR_GEOIP", default=False):
        options["geoip"] = True

    backend = os.environ.get("AUTOMATION_CONNECTOR_CLOAK_BACKEND")
    if backend:
        options["backend"] = backend

    return options


def _is_launchd_backend() -> bool:
    """True when the backend was started via the launchd plist (no GUI session)."""
    return os.environ.get("TOKYO_BACKEND_LAUNCHD") == "1"


def can_launch_visible_browser() -> bool:
    """Whether a headless=False launch is expected to result in a *visible* window on the desktop.

    When False (the launchd agent case), we short-circuit in the launch helpers with a
    crystal-clear actionable error instead of letting cloak launch fail deep or silently
    produce no window. This makes "press Watch, see the real popup of the human mechanics"
    reliable once the user follows the one-time terminal command.
    """
    if _is_launchd_backend():
        return False
    # Interactive terminal / normal user-started process on a desktop machine.
    # Allow override for CI or pure-headless test rigs.
    return _env_bool("AUTOMATION_CONNECTOR_ALLOW_VISIBLE", default=True)


def launch_automation_browser(*, headless: bool = True, **launch_kwargs: Any) -> Any:
    """Single controlled surface for launching a (non-persistent) CloakBrowser.

    All call sites that previously did `from cloakbrowser import launch` + manual locale/timezone/options
    should use this instead. This makes Cloak upgrades (API changes to launch, human_config keys,
    new required args within the 0.3.x pin) localized: only edit here + the pin in requirements/pyproject.

    The returned object is expected to support .new_context() / .new_page() / .close() (or be used
    with launch_persistent_context flows).
    """
    try:
        from cloakbrowser import launch  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "cloakbrowser is not installed in this runtime; install the automation dependency "
            "(see requirements.txt / pyproject.toml cloakbrowser>=0.3.31,<0.4) before browser automation."
        ) from exc

    if not headless and not can_launch_visible_browser():
        raise RuntimeError(
            "VISIBLE_BROWSER_NOT_AVAILABLE_IN_THIS_PROCESS: "
            "Watch / visible=over-the-shoulder requested a real headed browser window so you can watch the "
            "full human-like session (plan-driven arc/two-step moves + imprecision + overshoot, mouse-positioned "
            "wheel scrolls, persona pacing, your variation_guide side actions like peeks/hovers/expands/notifs/bidir, etc.) "
            "act out live.\n\n"
            "However, this backend process is the launchd agent (TOKYO_BACKEND_LAUNCHD=1) which has no graphical "
            "desktop session attached. Cloak launches with headless=False will not produce a window visible to you.\n\n"
            "Fix (copy-paste in a normal Terminal on your Mac):\n"
            "  cd /Users/john/repos/internal-app-tokyo\n"
            "  ./scripts/tokyo restart backend   # stops the launchd one\n"
            "  ./scripts/server.sh --foreground  # starts uvicorn *attached in your desktop/GUI session* (most reliable for popups)\n\n"
            "Then return to the UI and press Watch (or Check visibly) on the card again.\n"
            "A real browser window will pop on your desktop and perform exactly the configured behavior.\n\n"
            "When finished watching: Ctrl-C the --foreground server, then ./scripts/tokyo up backend\n\n"
            "(The launchd one is great for everything else; only visible Watch needs the interactive/foreground server for the popup.)\n"
            "Full details: server/automation_connector/ARCHITECTURE.md (search for 'Practical note for visible runs')."
        )

    opts = {**automation_connector_cloak_options(), **launch_kwargs}

    # Support extra Chromium args for advanced redteam/anti-fp (e.g. flags that
    # further reduce telltales). Passed via env so we don't hardcode in code.
    extra_args = os.environ.get("AUTOMATION_CONNECTOR_LAUNCH_ARGS")
    if extra_args:
        try:
            arg_list = [a.strip() for a in extra_args.split() if a.strip()]
            if arg_list:
                existing = opts.get("args") or []
                if isinstance(existing, (list, tuple)):
                    opts["args"] = list(existing) + arg_list
                else:
                    opts["args"] = arg_list
        except Exception:
            pass

    # Sanitize to prevent "multiple values for keyword argument" when callers
    # (or our own stealth_context_kwargs helper) pass locale/timezone/timezone_id.
    # The launch functions always control these for consistency.
    for k in ("locale", "timezone", "timezone_id"):
        opts.pop(k, None)

    if not headless:
        logger.info(
            "[automation_connector] launching VISIBLE (headed, headless=False) browser for over-the-shoulder / live visual inspection. "
            "A real browser window should appear on the desktop of the process running this backend (requires graphical session; "
            "launchd backend usually does not provide one — use ./scripts/server.sh --foreground for the most reliable popup)."
        )
    try:
        return launch(
            headless=headless,
            locale=automation_connector_locale(),
            timezone=automation_connector_timezone(),
            **opts,
        )
    except Exception as exc:
        if not headless:
            logger.exception("[automation_connector] visible (headless=False) Cloak browser launch raised an exception")
            low = (str(exc) + " " + getattr(exc, "__class__", type(exc)).__name__).lower()
            is_likely_display = any(k in low for k in ["display", "gui", "headless", "window", "nsapplication", "sandbox", "xpc", "no display", "cannot open display", "launch", "connect", "permission", "not allowed"])
            if _is_launchd_backend() or not can_launch_visible_browser() or is_likely_display:
                # Known no-GUI or display-related failure: give the actionable server.sh guidance
                raise RuntimeError(
                    "VISIBLE_BROWSER_NOT_AVAILABLE_IN_THIS_PROCESS: "
                    "Watch / visible=over-the-shoulder requested a real headed browser window so you can watch the "
                    "full human-like session (plan-driven arc/two-step moves + imprecision + overshoot, mouse-positioned "
                    "wheel scrolls, persona pacing, your variation_guide side actions like peeks/hovers/expands/notifs/bidir, etc.) "
                    "act out live.\n\n"
                    "The attempt to launch a visible (headless=False) browser failed (this process has no attached graphical desktop session — typically the launchd agent).\n\n"
                    "Fix (copy-paste in a normal Terminal on your Mac):\n"
                    "  cd /Users/john/repos/internal-app-tokyo\n"
                    "  ./scripts/tokyo restart backend   # stop the launchd one\n"
                    "  ./scripts/server.sh --foreground  # run uvicorn *attached* in your desktop/GUI session (best for popups)\n\n"
                    "Then return to the UI and press Watch (or Check visibly) on the card again.\n"
                    "A real browser window will pop on your desktop and perform exactly the configured behavior.\n\n"
                    "When finished watching: Ctrl-C the foreground server, then ./scripts/tokyo up backend\n\n"
                    "(launchd is fine for normal runs; only Watch visible observation needs the interactive/foreground server.)\n"
                    "Full details: server/automation_connector/ARCHITECTURE.md (search for 'Practical note for visible runs').\n\n"
                    f"Original launch error: {exc}"
                ) from exc
            else:
                # We were in a "should be GUI" context (e.g. server.sh or direct) but still got a launch error.
                # Do *not* mask it as the launchd message — surface the real cloak/Playwright problem.
                raise RuntimeError(
                    "CLOAK_HEADED_LAUNCH_FAILED: cloakbrowser launch(..., headless=False) failed even though this backend process "
                    "is not the launchd agent (can_launch_visible_browser() was True and TOKYO_BACKEND_LAUNCHD was not 1).\n\n"
                    "A real GUI session was expected (e.g. you ran ./scripts/server.sh or uvicorn directly from a Terminal), "
                    "but the headed browser still did not start. This is a cloak/Playwright launch problem (not the known no-display launchd case).\n\n"
                    f"Original error from cloak: {exc}\n\n"
                    "Recommended for reliable visible Watch sessions (copy-paste):\n"
                    "  cd /Users/john/repos/internal-app-tokyo\n"
                    "  ./scripts/tokyo restart backend\n"
                    "  ./scripts/server.sh --foreground\n"
                    "Then press Watch in the UI. The browser window running the *exact* plan + variations will appear.\n"
                    "Stop the foreground server with Ctrl-C when done.\n\n"
                    "Other checks: ensure Chrome is installed, run `playwright install` if needed, check backend logs for cloak details.\n"
                    "See server/automation_connector/ARCHITECTURE.md for the visible note."
                ) from exc
        raise


def launch_automation_persistent_context(profile_dir: str, *, headless: bool = True, **launch_kwargs: Any) -> Any:
    """Single controlled surface for launching a persistent CloakBrowser context (used for LinkedIn cookie/profile).

    Centralizes the import guard, options merge (humanize, human_config, proxy, geoip, backend, ...),
    and common locale/timezone so that Cloak version upgrades or launch_persistent_context signature
    changes are not scattered. Callers can still pass per-use kwargs (user_agent, etc) which override.
    """
    try:
        from cloakbrowser import launch_persistent_context  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "cloakbrowser is not installed in this runtime; install the automation dependency "
            "(see requirements.txt / pyproject.toml cloakbrowser>=0.3.31,<0.4) before browser automation."
        ) from exc

    if not headless and not can_launch_visible_browser():
        raise RuntimeError(
            "VISIBLE_BROWSER_NOT_AVAILABLE_IN_THIS_PROCESS: "
            "Watch / visible=over-the-shoulder requested a real headed browser window (persistent context for LinkedIn cookies etc) "
            "so you can watch the full human-like session live.\n\n"
            "However, this backend process is the launchd agent (TOKYO_BACKEND_LAUNCHD=1) which has no graphical "
            "desktop session attached. Cloak launches with headless=False will not produce a window visible to you.\n\n"
            "Fix (copy-paste in a normal Terminal on your Mac):\n"
            "  cd /Users/john/repos/internal-app-tokyo\n"
            "  ./scripts/tokyo restart backend   # stops the launchd one\n"
            "  ./scripts/server.sh --foreground  # starts uvicorn *attached in your desktop/GUI session* (most reliable for popups)\n\n"
            "Then return to the UI and press Watch (or Check visibly) on the card again.\n"
            "A real browser window will pop on your desktop and perform exactly the configured behavior (including profile peeks, scrolls, variations, etc.).\n\n"
            "When finished watching: Ctrl-C the --foreground server, then ./scripts/tokyo up backend\n\n"
            "(The launchd one is great for everything else; only visible Watch needs the interactive/foreground server for the popup.)\n"
            "Full details: server/automation_connector/ARCHITECTURE.md (search for 'Practical note for visible runs')."
        )

    opts = {**automation_connector_cloak_options(), **launch_kwargs}

    # Support extra Chromium args (same as non-persistent path).
    extra_args = os.environ.get("AUTOMATION_CONNECTOR_LAUNCH_ARGS")
    if extra_args:
        try:
            arg_list = [a.strip() for a in extra_args.split() if a.strip()]
            if arg_list:
                existing = opts.get("args") or []
                if isinstance(existing, (list, tuple)):
                    opts["args"] = list(existing) + arg_list
                else:
                    opts["args"] = arg_list
        except Exception:
            pass

    # Sanitize locale/timezone to avoid "got multiple values" when the caller
    # spreads automation_connector_stealth_context_kwargs() (which includes them).
    for k in ("locale", "timezone", "timezone_id"):
        opts.pop(k, None)

    if not headless:
        logger.info(
            "[automation_connector] launching VISIBLE (headed, headless=False) PERSISTENT browser context for over-the-shoulder / live visual inspection (LinkedIn etc). "
            "A real browser window should appear on the desktop of the process running this backend (requires graphical session; "
            "launchd backend usually does not provide one — use ./scripts/server.sh --foreground for the most reliable popup)."
        )
    try:
        return launch_persistent_context(
            profile_dir,
            headless=headless,
            locale=automation_connector_locale(),
            timezone=automation_connector_timezone(),
            **opts,
        )
    except Exception as exc:
        if not headless:
            logger.exception("[automation_connector] visible (headless=False) Cloak PERSISTENT context launch raised an exception")
            low = (str(exc) + " " + getattr(exc, "__class__", type(exc)).__name__).lower()
            is_likely_display = any(k in low for k in ["display", "gui", "headless", "window", "nsapplication", "sandbox", "xpc", "no display", "cannot open display", "launch", "connect", "permission", "not allowed"])
            if _is_launchd_backend() or not can_launch_visible_browser() or is_likely_display:
                raise RuntimeError(
                    "VISIBLE_BROWSER_NOT_AVAILABLE_IN_THIS_PROCESS: "
                    "Watch / visible=over-the-shoulder requested a real headed browser window (persistent context for LinkedIn cookies etc) so you can watch the "
                    "full human-like session live.\n\n"
                    "The attempt to launch a visible (headless=False) browser failed (this process has no attached graphical desktop session — typically the launchd agent).\n\n"
                    "Fix (copy-paste in a normal Terminal on your Mac):\n"
                    "  cd /Users/john/repos/internal-app-tokyo\n"
                    "  ./scripts/tokyo restart backend   # stop the launchd one\n"
                    "  ./scripts/server.sh --foreground  # run uvicorn *attached* in your desktop/GUI session (best for popups)\n\n"
                    "Then return to the UI and press Watch (or Check visibly) on the card again.\n"
                    "A real browser window will pop on your desktop and perform exactly the configured behavior (including profile peeks, scrolls, variations, etc.).\n\n"
                    "When finished watching: Ctrl-C the foreground server, then ./scripts/tokyo up backend\n\n"
                    "(launchd is fine for normal runs; only Watch visible observation needs the interactive/foreground server.)\n"
                    "Full details: server/automation_connector/ARCHITECTURE.md (search for 'Practical note for visible runs').\n\n"
                    f"Original launch error: {exc}"
                ) from exc
            else:
                raise RuntimeError(
                    "CLOAK_HEADED_LAUNCH_FAILED (persistent): cloakbrowser launch_persistent_context(..., headless=False) failed even though this backend process "
                    "is not the launchd agent (can_launch_visible_browser() was True).\n\n"
                    "A real GUI session was expected, but the headed browser still did not start. This is a cloak/Playwright problem (not the no-display launchd case).\n\n"
                    f"Original error from cloak: {exc}\n\n"
                    "Recommended for reliable visible Watch sessions (copy-paste):\n"
                    "  cd /Users/john/repos/internal-app-tokyo\n"
                    "  ./scripts/tokyo restart backend\n"
                    "  ./scripts/server.sh --foreground\n"
                    "Then press Watch. The browser window will appear and run the feed observation with all the human mechanics + your variation_guide.\n"
                    "Stop with Ctrl-C when done.\n\n"
                    "Other checks: Chrome installed? playwright install? backend logs.\n"
                    "See server/automation_connector/ARCHITECTURE.md for the visible note."
                ) from exc
        raise


def automation_connector_stealth_context_kwargs() -> dict[str, Any]:
    """Return a realistic desktop browser context profile for automation runs.

    This is the "anti-fingerprint hygiene" layer for the connector:
    - A normal desktop UA (not a headless/playwright giveaway)
    - Desktop viewport + scale (matches what a human would have)
    - Proper locale/timezone (from our controlled envs, not the container default)

    All new_context() sites for automation (generic skim, linkedin persistent,
    visible Watch, the observe harness, browser_sessions login etc.) should
    use this (spread into **ctx_kwargs) so the created context looks like an
    ordinary desktop Chromium on a Mac.

    The visible synthetic cursor (for movies / over-the-shoulder) is a separate
    QA-only DOM overlay and does not affect this stealth profile.

    Camoufox is also supported as an alternative runner (via env) and provides
    its own strong anti-detect defaults; the context kwargs here are still
    applied for consistency.
    """
    # A representative recent desktop UA. Can be varied per-persona in future
    # if we want even more distribution, but this + cloak's other signals is
    # already much better than raw playwright.
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    return {
        "user_agent": ua,
        "viewport": {"width": 1440, "height": 900},
        "device_scale_factor": 2,
        "is_mobile": False,
        "has_touch": False,
        "locale": automation_connector_locale(),
        "timezone_id": automation_connector_timezone(),
    }


def apply_anti_fingerprint_init_script(context: Any) -> None:
    """Inject a tiny, defensive JS init script on the context to hide automation tells.

    - Force navigator.webdriver to report false (or be absent)
    - Remove cdc_... properties that some Playwright wrappers leave behind
    - A minimal toString guard so the patched getter doesn't look "redefined by automation"

    This runs for *every* automation context (via the launch helpers and the
    browser_sessions / scraper / checker / harness paths). It is intentionally
    small and never throws into page JS.

    The synthetic red cursor (for visual QA of human moves) is injected only
    on visible= paths via interaction_primitives and uses private __human_cursor__
    names; it is not present for normal runs and is not intended as stealth
    (its whole point is to be obvious in the movie).

    If you are concerned about "advertising hidden playwright", this + the
    stealth context kwargs + cloak's own anti-detect (and the Camoufox option)
    is the controlled surface. Do not add more obvious patches elsewhere.
    """
    js = """
    (function(){
        try {
            // Remove some common automation / driver artifacts if the wrapper left any
            try { delete window['cdc_']; } catch(e){}
            try { delete window['cdc_adoQpoasnfa76pfcZLmcfl']; } catch(e){}

            // Define custom webdriver getter on Navigator.prototype to prevent prototype leaks
            const customWebdriverGetter = function get webdriver() {
                return false;
            };
            if ('webdriver' in navigator) {
                Object.defineProperty(Navigator.prototype, 'webdriver', {
                    get: customWebdriverGetter,
                    configurable: true
                });
            }

            // Bulletproof toString override that handles custom toString and webdriver getter perfectly
            const origToString = Function.prototype.toString;
            const customToString = function toString() {
                if (this === customToString) {
                    return 'function toString() { [native code] }';
                }
                if (this === customWebdriverGetter) {
                    return 'function get webdriver() { [native code] }';
                }
                return origToString.call(this);
            };

            Object.defineProperty(Function.prototype, 'toString', {
                value: customToString,
                writable: true,
                enumerable: false,
                configurable: true
            });
        } catch (e) {
            // never break the page
        }
    })();
    """
    try:
        context.add_init_script(js)
    except Exception:
        pass
