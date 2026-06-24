"""Regression tests for the feed scroll/positioning detectability findings (2026-06-23).

The Blue Team observed the feed walk as: every scroll the SAME magnitude (no
randomness), the cursor pinned to a centre box flick after flick, and no
content engagement. Root causes pinned here:

- the semi-Markov feed engine samples a fresh randomized per-flick wheel delta,
  but ``scroll_page`` routed it through the plan's precomputed pacing list, which
  — indexed by an ever-growing step counter — CLAMPED to its last element, so
  every feed scroll became one identical magnitude. The engine now passes
  ``honor_fallback`` so its per-flick delta is used verbatim (``scroll_page``);
- ``_position_mouse_over_content`` aimed at a narrow centre band of the first
  content element before EVERY scroll, pinning the cursor to a centre box. It
  now varies the post + spread and parks off toward a rail a good share of the
  time;
- when no ``feed_post`` resolves, every content gesture silently no-ops and the
  session degrades to a scroll-only walk. ``feed_scan_cycle`` now warns + records
  ``feed_post_unresolved`` once so the failure is visible, not hidden.
"""
from __future__ import annotations

import random
import types

from imposter5.automation_connector import interaction_primitives as ip
from imposter5.loaders import feed_actions as fa
from imposter5.loaders import markov_simulator as ms


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, action: str, metadata: dict | None = None) -> None:
        self.events.append((action, metadata or {}))


class _WheelPage:
    viewport_size = {"width": 1440, "height": 900}

    def __init__(self) -> None:
        self.wheels: list[int] = []

        class _Mouse:
            def __init__(self, sink: list[int]) -> None:
                self._sink = sink

            def wheel(self, _dx, dy):
                self._sink.append(dy)

            def move(self, _x, _y):
                pass

        self.mouse = _Mouse(self.wheels)

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, *_a, **_k):
        return None


# --------------------------------------------------------------------------- #
# Fix #1 — feed scroll honors the engine's fresh per-flick delta (no clamp)
# --------------------------------------------------------------------------- #
def test_honor_fallback_uses_verbatim_delta_even_with_pacing_list():
    """With honor_fallback the recorded delta == the supplied fallback, ignoring
    the plan's pacing list (which would otherwise clamp to its last element)."""
    page = _WheelPage()
    rec = _Recorder()
    # A short pacing list is exactly what made every scroll clamp to 1281-style
    # constants once pass_index overran it.
    plan = {"session_seed": "hf", "pacing": {"scroll_delta_y": [300, 1281]}}
    supplied = [210, 365, 442, 288, 470, 333, 199, 410]
    for i, d in enumerate(supplied):
        ip.scroll_page(page, plan, pass_index=i, fallback_delta_y=d, recorder=rec, honor_fallback=True)
    deltas = [m["delta_y"] for a, m in rec.events if a == "scroll"]
    assert deltas == supplied                      # verbatim, per-flick
    assert len(set(deltas)) > 1                     # genuinely varied, not constant


def test_pacing_list_clamps_to_last_without_honor_fallback():
    """Document the regression: without honor_fallback a short pacing list pegs
    every scroll past the list length to its LAST element — a constant magnitude."""
    page = _WheelPage()
    rec = _Recorder()
    plan = {"session_seed": "clamp", "pacing": {"scroll_delta_y": [300, 1281]}}
    for i in range(8):
        ip.scroll_page(page, plan, pass_index=i, fallback_delta_y=400, recorder=rec)
    deltas = [m["delta_y"] for a, m in rec.events if a == "scroll"]
    assert deltas[0] == 300
    assert all(d == 1281 for d in deltas[1:]), "pre-fix path clamps to the last pacing element"


# --------------------------------------------------------------------------- #
# Fix #1b — scroll burst rhythm: a run of flicks per reposition, not 1:1 metronome
# --------------------------------------------------------------------------- #
def test_scroll_feed_flicks_fires_a_burst_then_one_reposition():
    """``scroll_feed_flicks`` fires a RUN of 2-5 wheel flicks in a single call (the
    cursor repositions once at the start of the burst, not once per scroll), records
    each flick as its own scroll event, and bounds every flick to a human wheel
    notch — the fix for the metronomic one-scroll-one-move tell."""
    page = _WheelPage()
    rec = _Recorder()
    result = ip.scroll_feed_flicks(
        page, {"session_seed": "flick-burst"}, pass_index=0, recorder=rec, min_flicks=2, max_flicks=5
    )
    scrolls = [m for a, m in rec.events if a == "scroll"]
    assert 2 <= len(scrolls) <= 5                       # a burst, not a single scroll
    assert len(scrolls) == result["flicks"]
    assert result["delta_y"] == sum(page.wheels)
    assert all(140 <= d <= 520 for d in page.wheels)    # bounded human wheel notches
    assert [m["flick"] for m in scrolls] == list(range(len(scrolls)))
    assert all(m["pass_index"] == 0 for m in scrolls)


def test_scroll_feed_flicks_is_deterministic_per_seed():
    """Seeded by the plan, a burst is replayable: same seed + pass_index -> identical
    flick count and deltas, so sessions reproduce without being globally constant."""
    plan = {"session_seed": "repeat-me"}
    a = ip.scroll_feed_flicks(_WheelPage(), plan, pass_index=1, min_flicks=1, max_flicks=5)
    b = ip.scroll_feed_flicks(_WheelPage(), plan, pass_index=1, min_flicks=1, max_flicks=5)
    assert a == b


def test_markov_feed_scroll_deltas_are_not_constant():
    """End-to-end: the ambient walk forced into scroll_down emits VARIED wheel
    magnitudes, even though the plan carries a clamping pacing list."""
    page = _WheelPage()
    rec = _Recorder()
    plan = {
        "session_seed": "varied",
        # The clamping pacing list is present; the engine must bypass it.
        "pacing": {"scroll_delta_y": [1281]},
        "markov_matrix": {
            "idle": {"scroll_down": 1.0},
            "scroll_down": {"scroll_down": 1.0},
        },
    }
    ms.run_markov_simulation(
        page, plan, max_steps=40, initial_state="scroll_down", suppress_intro_wait=True,
        recorder=rec,
    )
    deltas = [m["delta_y"] for a, m in rec.events if a == "scroll"]
    assert len(deltas) >= 5, "expected several scroll flicks"
    # The pre-fix bug made these all 1281; the per-flick fallback (200-480) now wins.
    assert len(set(deltas)) > 1, f"feed scroll magnitudes must vary, got {set(deltas)}"
    assert all(abs(d) <= 600 for d in deltas), "honored per-flick deltas, not the 1281 pacing value"


# --------------------------------------------------------------------------- #
# Fix #2 — pre-scroll cursor is not pinned to a centre box
# --------------------------------------------------------------------------- #
def test_position_mouse_over_content_is_not_centre_pinned(monkeypatch):
    targets: list[tuple[float, float]] = []
    monkeypatch.setattr(
        ip, "_safe_mouse_move",
        lambda page, x, y, plan, rng, recorder=None: targets.append((x, y)),
    )

    class _Page:
        def set_default_timeout(self, _ms):
            pass

        # One centred content rect (the only thing the pre-fix motor ever aimed at).
        def evaluate(self, *_a, **_k):
            return {
                "rects": [{"left": 520.0, "top": 240.0, "w": 360.0, "h": 420.0}],
                "vw": 1440.0, "vh": 900.0, "header": 64.0,
            }

    rng = random.Random(11)
    for _ in range(240):
        ip._position_mouse_over_content(_Page(), {}, rng)
    assert targets, "expected pre-scroll positioning moves"
    xs = [x / 1440.0 for x, _ in targets]
    # A meaningful share parks off toward a rail/gutter (breaks the centre box)...
    assert any(x < 0.20 for x in xs)
    assert any(x > 0.80 for x in xs)
    # ...while still keeping plenty of moves over the content column.
    assert sum(1 for x in xs if 0.30 <= x <= 0.70) > 0


def test_position_mouse_over_content_varied_without_content(monkeypatch):
    """No resolvable content => a VARIED viewport point, never a fixed centre."""
    targets: list[tuple[float, float]] = []
    monkeypatch.setattr(
        ip, "_safe_mouse_move",
        lambda page, x, y, plan, rng, recorder=None: targets.append((x, y)),
    )

    class _Page:
        def set_default_timeout(self, _ms):
            pass

        def evaluate(self, *_a, **_k):
            return {"rects": [], "vw": 1440.0, "vh": 900.0, "header": 64.0}

    rng = random.Random(5)
    for _ in range(120):
        ip._position_mouse_over_content(_Page(), {}, rng)
    xs = {round(x, 1) for x, _ in targets}
    ys = {round(y, 1) for _, y in targets}
    assert len(xs) > 5 and len(ys) > 5, "fallback point must not be a fixed coordinate"


# --------------------------------------------------------------------------- #
# Fix #3 — an unreadable feed is surfaced loudly, not silently scrolled
# --------------------------------------------------------------------------- #
def _make_feed_session(recorder, *, feed_post_resolves: bool) -> fa.FeedSession:
    class _Resolver:
        def selector_for(self, _role):
            return "article" if feed_post_resolves else None

        def all(self, _role, limit=1):
            return [object()] if feed_post_resolves else []

    return fa.FeedSession(
        page=object(), plan={}, recorder=recorder, resolver=_Resolver(),
        interest_terms=[], scorer=types.SimpleNamespace(submit=lambda posts: None),
        summary={"feed_scan_bursts": 0, "markov_steps": 0, "posts_captured": 0, "profile": "linkedin"},
    )


def _neuter_cycle(monkeypatch):
    """Make the heavy feed primitives no-ops so we exercise only the diagnostic."""
    monkeypatch.setattr(fa, "scan_feed_burst", lambda fs, steps=None: None)
    monkeypatch.setattr(fa, "capture_visible_posts", lambda fs, sink=None: 0)
    monkeypatch.setattr(fa, "act_on_scored_posts", lambda fs: False)
    monkeypatch.setattr(fa, "peek_post_engagement", lambda fs: False)
    monkeypatch.setattr(fa, "_sprinkle_engagement", lambda fs: None)
    monkeypatch.setattr(fa, "like_interesting_post", lambda fs: False)


def test_feed_scan_warns_once_when_no_feed_post(monkeypatch):
    _neuter_cycle(monkeypatch)
    rec = _Recorder()
    fs = _make_feed_session(rec, feed_post_resolves=False)
    fa.feed_scan_cycle(fs, steps=1)
    fa.feed_scan_cycle(fs, steps=1)
    warns = [m for a, m in rec.events if a == "feed_post_unresolved"]
    assert len(warns) == 1, "unreadable feed must be surfaced exactly once"
    assert fs.summary.get("feed_post_unresolved") is True


def test_feed_scan_silent_when_feed_post_resolves(monkeypatch):
    _neuter_cycle(monkeypatch)
    rec = _Recorder()
    fs = _make_feed_session(rec, feed_post_resolves=True)
    fa.feed_scan_cycle(fs, steps=1)
    assert not any(a == "feed_post_unresolved" for a, _ in rec.events)
    assert not fs.summary.get("feed_post_unresolved")
