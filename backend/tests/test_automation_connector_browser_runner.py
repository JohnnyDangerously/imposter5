from __future__ import annotations

import sys

import pytest

from imposter5.automation_connector.browser_runner import (
    BrowserRunnerUnavailable,
    CamoufoxBrowserRunner,
    CloakBrowserRunner,
    get_browser_runner,
)


def test_default_browser_runner_is_cloak(monkeypatch) -> None:
    monkeypatch.delenv("AUTOMATION_CONNECTOR_BROWSER_RUNNER", raising=False)

    runner = get_browser_runner()

    assert isinstance(runner, CloakBrowserRunner)


def test_browser_runner_can_select_camoufox() -> None:
    runner = get_browser_runner("camoufox")

    assert isinstance(runner, CamoufoxBrowserRunner)


def test_camoufox_runner_is_guarded_when_package_missing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", None)

    with pytest.raises(BrowserRunnerUnavailable, match="camoufox runner is not installed"):
        CamoufoxBrowserRunner().launch_browser(headless=True)


def test_unknown_browser_runner_rejects_cleanly() -> None:
    with pytest.raises(BrowserRunnerUnavailable, match="unknown automation browser runner"):
        get_browser_runner("surprise")
