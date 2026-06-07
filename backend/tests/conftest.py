from __future__ import annotations

import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = REPO / "harness" / "fixtures" / "gauntlet_fixture.html"


@pytest.fixture()
def _gauntlet_browser():
    """Yield a headless Chromium, skipping (not failing) when unavailable.

    Skips so the pure-logic story tests still run in environments without Playwright
    browser binaries installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"playwright not importable: {exc}")

    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover - browser binary missing
        if browser is not None:
            browser.close()
        if pw is not None:
            pw.stop()
        pytest.skip(f"chromium unavailable: {exc}")

    try:
        yield browser
    finally:
        browser.close()
        pw.stop()


def _new_gauntlet_page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    # Bound auto-wait so a momentarily non-actionable element fails fast in tests
    # instead of blocking on Playwright's 30s default (the executor swallows the
    # failure honestly). Production runs use their own page without this cap.
    page.set_default_timeout(4000)
    page.goto(FIXTURE.as_uri(), wait_until="domcontentloaded")
    return context, page


@pytest.fixture()
def gauntlet_page(_gauntlet_browser):
    """Yield a single fresh Playwright page loaded with the local gauntlet fixture."""
    context, page = _new_gauntlet_page(_gauntlet_browser)
    try:
        yield page
    finally:
        context.close()


@pytest.fixture()
def gauntlet_page_factory(_gauntlet_browser):
    """Yield a factory that returns a FRESH gauntlet page (fresh context) each call.

    Lets a test run several independent story attempts under clean browser state,
    matching how production runs one session per page.
    """
    contexts = []

    def _make():
        context, page = _new_gauntlet_page(_gauntlet_browser)
        contexts.append(context)
        return page

    try:
        yield _make
    finally:
        for ctx in contexts:
            try:
                ctx.close()
            except Exception:
                pass
