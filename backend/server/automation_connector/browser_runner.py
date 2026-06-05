"""Browser runner boundary for automation connector providers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from loaders.cloak_runtime import automation_connector_humanize_enabled


class BrowserRunnerUnavailable(RuntimeError):
    """Raised when a configured browser runner is not available locally."""


class BrowserRunner(Protocol):
    """Minimal sync runner interface consumed by provider adapters."""

    name: str

    def launch_browser(self, *, headless: bool = True) -> Any:
        """Launch and return a browser-like object."""


@dataclass(frozen=True)
class CloakBrowserRunner:
    """CloakBrowser-backed runner used by the connector today."""

    name: str = "cloak"

    def launch_browser(self, *, headless: bool = True) -> Any:
        # Use the centralized launcher (in loaders/cloak_runtime) so Cloak API changes or
        # human_config evolution are not duplicated. The runner still provides the swappable
        # BrowserRunner boundary for camoufox vs cloak selection.
        from loaders.cloak_runtime import launch_automation_browser

        try:
            return launch_automation_browser(headless=headless)
        except RuntimeError as exc:
            raise BrowserRunnerUnavailable(str(exc)) from exc


class _CamoufoxBrowserHandle:
    """Adapt the Camoufox context manager to the new_context/new_page/close
    surface the connector's providers expect."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._browser = manager.__enter__()

    def new_context(self, *args: Any, **kwargs: Any) -> Any:
        return self._browser.new_context(*args, **kwargs)

    def new_page(self, *args: Any, **kwargs: Any) -> Any:
        return self._browser.new_page(*args, **kwargs)

    def close(self) -> None:
        self._manager.__exit__(None, None, None)


@dataclass(frozen=True)
class CamoufoxBrowserRunner:
    """Camoufox-backed runner, selectable via AUTOMATION_CONNECTOR_BROWSER_RUNNER.

    Launches Camoufox with its default settings and exposes the same
    new_context/new_page/close surface as the other runners.
    """

    name: str = "camoufox"

    def launch_browser(self, *, headless: bool = True) -> Any:
        try:
            from camoufox.sync_api import Camoufox  # type: ignore[import]
        except ImportError as exc:
            raise BrowserRunnerUnavailable(
                "camoufox runner is not installed; run `pip install camoufox` or use the cloak runner"
            ) from exc
        opts: dict[str, Any] = {}
        if automation_connector_humanize_enabled():
            opts["humanize"] = True
        return _CamoufoxBrowserHandle(Camoufox(headless=headless, **opts))


def get_browser_runner(name: str | None = None) -> BrowserRunner:
    """Return the configured browser runner."""
    import os

    runner_name = (name or os.environ.get("AUTOMATION_CONNECTOR_BROWSER_RUNNER") or "cloak").strip().lower()
    if runner_name in {"cloak", "cloakbrowser"}:
        return CloakBrowserRunner()
    if runner_name == "camoufox":
        return CamoufoxBrowserRunner()
    raise BrowserRunnerUnavailable(f"unknown automation browser runner: {runner_name}")
