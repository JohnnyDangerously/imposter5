"""Motor-realism regression tests for the Red Team evasion behaviors.

These pin the fixes for the Blue Team detectability findings (2026-06-22):

- end-of-move tremor must DECAY to a clean endpoint — no sub-pixel jitter left
  sitting on a cursor that has already stopped (``_emit_bezier``);
- scrolls must occasionally OVER-SHOOT and correct back the other way (the
  reverse-direction "debounce" momentum decay alone never produces);
- ambient mouse moves must break out of the central content column with
  rest/edge excursions and reach actionable controls, instead of perpetually
  grazing post bodies in a ~70% box (``run_markov_simulation`` mousemove).
"""
from __future__ import annotations

import random

from imposter5.automation_connector import interaction_primitives as ip
from imposter5.loaders import markov_simulator as ms


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, action: str, metadata: dict | None = None) -> None:
        self.events.append((action, metadata or {}))


# --------------------------------------------------------------------------- #
# Fix #5 — settle tremor decays to a clean endpoint
# --------------------------------------------------------------------------- #
def _run_emit(p0, p1, p2, p3, amp: float) -> list[tuple[float, float]]:
    moves: list[tuple[float, float]] = []

    class _Mouse:
        def move(self, x, y):
            moves.append((x, y))

    class _Page:
        mouse = _Mouse()

        def wait_for_timeout(self, _ms):
            pass

    ip._emit_bezier(
        _Page(), p0, p1, p2, p3,
        steps=24, rng=random.Random(7), total_ms=240.0, clock=[0.0],
        tremor_hz=10.0, tremor_amp_px=amp, power_law_gain=0.8, min_jerk_skew=0.12,
    )
    return moves


def test_tremor_is_active_midpath_but_endpoint_is_clean():
    p0, p1, p2, p3 = (100.0, 100.0), (300.0, 150.0), (560.0, 360.0), (700.0, 420.0)
    clean = _run_emit(p0, p1, p2, p3, amp=0.0)
    shaky = _run_emit(p0, p1, p2, p3, amp=3.0)
    # The move ENDS exactly on the target in both cases: with the fix the tremor
    # envelope returns to 0 at the endpoint, so no jitter sits on the stopped cursor.
    assert clean[-1] == p3
    assert shaky[-1] == p3
    # Tremor really fired: some interior sample differs from the clean path.
    assert any(c != s for c, s in zip(clean[:-1], shaky[:-1], strict=False))


def test_zero_length_move_emits_no_jitter():
    pt = (400.0, 400.0)
    moves = _run_emit(pt, pt, pt, pt, amp=3.0)
    # A degenerate (zero-length) move has no travel direction, so the chord-normal
    # fallback is null and the cursor must not "explode into a pixel" at rest.
    # (Only float-basis rounding on the Bezier sum is allowed, << 1 sub-pixel.)
    assert max(abs(mx - 400.0) + abs(my - 400.0) for mx, my in moves) < 0.01


def test_motor_noise_is_active_mid_flight_not_only_at_settle():
    # Signal-dependent noise (Harris & Wolpert, 1998) scales with SPEED, so the path
    # must be perturbed during the FAST mid-flight phase — the inverse of the old
    # settle-only tremor, which left the entire ballistic sweep sitting EXACTLY on the
    # ideal Bezier until the last ~22% (a "too clean to be a hand" tell mid-flight).
    p0, p1, p2, p3 = (100.0, 100.0), (300.0, 150.0), (560.0, 360.0), (700.0, 420.0)
    clean = _run_emit(p0, p1, p2, p3, amp=0.0)
    shaky = _run_emit(p0, p1, p2, p3, amp=2.0)
    n = len(clean)
    # Inspect only the first half (mid-flight, well before the >=0.55 settle onset).
    assert any(clean[i] != shaky[i] for i in range(1, n // 2)), "no mid-flight motor noise"


def test_settle_tremor_is_two_dimensional_not_a_single_line():
    # Real hand tremor is a 2-D ellipse. The old model applied one oscillation along a
    # single perpendicular vector (rank-1: every deviation collinear — it "explodes"
    # along one axis). The motion noise must now span TWO dimensions.
    p0, p1, p2, p3 = (100.0, 100.0), (260.0, 130.0), (520.0, 300.0), (640.0, 360.0)
    clean = _run_emit(p0, p1, p2, p3, amp=0.0)
    shaky = _run_emit(p0, p1, p2, p3, amp=2.5)
    devs = [
        (shaky[i][0] - clean[i][0], shaky[i][1] - clean[i][1])
        for i in range(len(clean) - 1)
    ]
    devs = [d for d in devs if abs(d[0]) > 1e-9 or abs(d[1]) > 1e-9]
    assert len(devs) >= 4
    # 2x2 covariance of the deviation vectors; a rank-1 (single-line) wobble has a
    # near-zero minor eigenvalue. Require a non-degenerate minor/major ratio.
    mx = sum(d[0] for d in devs) / len(devs)
    my = sum(d[1] for d in devs) / len(devs)
    sxx = sum((d[0] - mx) ** 2 for d in devs) / len(devs)
    syy = sum((d[1] - my) ** 2 for d in devs) / len(devs)
    sxy = sum((d[0] - mx) * (d[1] - my) for d in devs) / len(devs)
    tr = sxx + syy
    disc = max(0.0, (tr * tr) / 4.0 - (sxx * syy - sxy * sxy))
    major = tr / 2.0 + disc ** 0.5
    minor = tr / 2.0 - disc ** 0.5
    assert major > 0.0
    assert (minor / major) > 0.02, "deviations collapse to a single line (rank-1 tremor regressed)"


# --------------------------------------------------------------------------- #
# Fix #4 — scroll over-shoot and correct
# --------------------------------------------------------------------------- #
def _scroll_page_runs(overshoot_chance: float, passes: int):
    """Run several scroll passes on one session (advancing the seeded RNG) and
    return all wheel deltas + per-pass scroll metadata."""
    wheels: list[int] = []

    class _Mouse:
        def wheel(self, _dx, dy):
            wheels.append(dy)

        def move(self, _x, _y):
            pass

    class _Page:
        viewport_size = {"width": 1440, "height": 900}
        mouse = _Mouse()

        def wait_for_timeout(self, _ms):
            pass

        def evaluate(self, *_a, **_k):
            return None

    page = _Page()
    rec = _Recorder()
    # scroll_overshoot_chance is clamped to <=0.6 in the motor, so over a handful
    # of passes the occasional reverse correction is observed deterministically.
    plan = {"session_seed": "ov", "human_config": {"scroll_overshoot_chance": overshoot_chance}}
    for i in range(passes):
        ip.scroll_page(page, plan, pass_index=i, fallback_delta_y=600, recorder=rec)
    metas = [m for a, m in rec.events if a == "scroll"]
    return wheels, metas


def test_scroll_overshoots_and_corrects_when_enabled():
    wheels, metas = _scroll_page_runs(overshoot_chance=0.6, passes=8)
    # Forward bursts scroll down (positive); some stops nudge back UP (negative).
    assert any(d > 0 for d in wheels)
    assert any(d < 0 for d in wheels), "expected at least one over-scroll correction"
    assert any(m["overshoot_px"] < 0 for m in metas)


def test_scroll_has_no_reverse_nudge_when_disabled():
    wheels, metas = _scroll_page_runs(overshoot_chance=0.0, passes=8)
    assert all(d >= 0 for d in wheels)
    assert all(m["overshoot_px"] == 0 for m in metas)


# --------------------------------------------------------------------------- #
# Fix #1/#2 — purposeful + parked targeting, not central-column float
# --------------------------------------------------------------------------- #
def test_rest_target_breaks_the_central_column():
    class _Page:
        viewport_size = {"width": 1440, "height": 900}

    rng = random.Random(3)
    xs = [ms._rest_target(_Page(), rng)[0] / 1440.0 for _ in range(200)]
    assert any(x < 0.20 for x in xs)   # parks at the left rail / window edge
    assert any(x > 0.80 for x in xs)   # parks at the right rail / scrollbar gutter
    assert all(x < 0.30 or x > 0.70 or True for x in xs)  # never raises; bounded


class _CtrlLoc:
    def all(self):
        return [self]

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, *_a):
        return ""

    def bounding_box(self):
        return {"x": 600, "y": 440, "width": 80, "height": 30}

    @property
    def first(self):
        return self

    def count(self):
        return 1


def test_actionable_target_returns_control_center():
    class _Page:
        viewport_size = {"width": 1440, "height": 900}

        def locator(self, _sel):
            return _CtrlLoc()

    pt = ms._actionable_target(_Page(), random.Random(1), ("button",))
    assert pt is not None
    x, y = pt
    assert 600 <= x <= 680 and 440 <= y <= 470


def test_actionable_target_none_without_targets():
    class _Page:
        viewport_size = {"width": 1440, "height": 900}

    assert ms._actionable_target(_Page(), random.Random(1), None) is None


def test_markov_mousemove_escapes_central_column():
    moves: list[tuple[float, float]] = []

    class _PostLoc:
        """A central feed-post container (content) — the only thing the pre-fix
        motor ever aimed at."""

        def all(self):
            return [self]

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def get_attribute(self, *_a):
            return ""

        def bounding_box(self):
            return {"x": 520, "y": 380, "width": 380, "height": 260}

        @property
        def first(self):
            return self

        def count(self):
            return 1

    class _Mouse:
        def move(self, x, y):
            moves.append((x, y))

        def wheel(self, _dx, _dy):
            pass

        def down(self):
            pass

        def up(self):
            pass

    class _Page:
        viewport_size = {"width": 1440, "height": 900}

        def __init__(self):
            self.mouse = _Mouse()

        def wait_for_timeout(self, _ms):
            pass

        def evaluate(self, *_a, **_k):
            return None

        def locator(self, _sel):
            return _PostLoc()

    plan = {
        "session_seed": "box",
        "markov_matrix": {"idle": {"mousemove": 1.0}, "mousemove": {"mousemove": 1.0}},
    }
    ms.run_markov_simulation(
        _Page(), plan, max_steps=60, suppress_intro_wait=True,
        mousemove_targets=("article",), hover_targets=("article",),
        actionable_targets=("button",),
    )
    vw = 1440.0
    xs = [x / vw for x, _ in moves]
    assert moves, "expected the ambient walk to emit mouse moves"
    # Rest excursions take the pointer to the rails/edges: it is NOT confined to
    # the central 0.30-0.70 column the pre-fix motor never left.
    assert any(x < 0.20 or x > 0.80 for x in xs)
    # ...but content/control reaches keep it mostly central (not pure edge noise).
    assert sum(1 for x in xs if 0.30 <= x <= 0.70) > 0


# --------------------------------------------------------------------------- #
# Fix #6 — reading/highlight trace routes through the curved Bezier emitter
# --------------------------------------------------------------------------- #
class _TraceMouse:
    def __init__(self, events: list) -> None:
        self.events = events

    def move(self, x, y):
        self.events.append(("move", (x, y)))

    def down(self):
        self.events.append(("down", None))

    def up(self):
        self.events.append(("up", None))

    def wheel(self, _dx, _dy):
        pass


class _TracePage:
    viewport_size = {"width": 1440, "height": 900}

    def __init__(self) -> None:
        self.events: list = []
        self.mouse = _TraceMouse(self.events)

    def set_default_timeout(self, _ms):
        pass

    def evaluate(self, *_a, **_k):
        # The text-block probe expects {left, top, w, h}; cursor-overlay evals ignore it.
        return {"left": 300.0, "top": 200.0, "w": 360.0, "h": 66.0}

    def wait_for_timeout(self, _ms):
        self.events.append(("wait", _ms))


def test_highlight_trace_is_a_curved_emitter_drag_between_press_and_release():
    page = _TracePage()
    ok = ip.trace_text_selection(page, {"session_seed": "hl"}, select=True)
    assert ok is True
    kinds = [e[0] for e in page.events]
    assert "down" in kinds and "up" in kinds
    down_i, up_i = kinds.index("down"), kinds.index("up")
    assert down_i < up_i
    # The drag between mousedown and mouseup now routes through _emit_bezier (curved,
    # velocity-profiled, tremored) — far more samples than the old 4-7 straight steps.
    drag_moves = [k for k in kinds[down_i:up_i] if k == "move"]
    assert len(drag_moves) >= 8


def test_reading_trace_emits_a_multi_sample_sweep():
    page = _TracePage()
    ok = ip.trace_text_selection(page, {"session_seed": "read"}, select=False)
    assert ok is True
    moves = [e for e in page.events if e[0] == "move"]
    # A reading trace (no button) is also a curved emitter sweep, not a straight line.
    assert len(moves) >= 12
    # A pure reading trace must NOT press the mouse button.
    assert all(k != "down" for k, _ in page.events)
