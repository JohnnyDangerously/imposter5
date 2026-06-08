"""Run the security partner's REAL fp-agent XGBoost behavioral classifier on a
session's mus.js frames, and return its verdict.

This is NOT a heuristic and NOT Blue's statistical Layer 3. It is the partner's
own trained model (`classifier_training/classifiers/*.json`) fed the exact
`BehavioralFV` feature vector their pipeline produces from mus.js frames
(`["m", x, y, t]` moves etc.). We default to the *all-classes* behavioral model
because it includes a `Human` class, so it can render a real human-vs-agent
verdict rather than only attributing to the closest known automation tool.

Class index order = alphabetical sort of class names (their
`AgentClassificationDataset.create_label_mapping`).
"""
from __future__ import annotations

import os
import pathlib
import sys
from typing import Any

HARNESS = pathlib.Path(__file__).resolve().parent
RED_REPO = HARNESS.parent
FP_AGENT = RED_REPO / "backend" / "src" / "imposter5" / "fp_agent"
CT = FP_AGENT / "classifier_training"
CT_SRC = CT / "src"
MUS_JS = HARNESS / "vendor" / "mus.js"

# Alphabetically-sorted class names (matches create_label_mapping). The model's
# predict() index maps into one of these by length of the probability vector.
ALL_CLASSES_LABELS = [
    "Atlas Agent", "Browser Use", "ChatGPT Agent", "Claude",
    "Comet", "Human", "Manus", "Skyvern",
]
BROWSING_ONLY_LABELS = [
    "Atlas Agent", "Browser Use", "ChatGPT Agent", "Claude",
    "Comet", "Manus", "Skyvern",
]

DEFAULT_MODEL = "behavioral_fingerprint_all_classes.json"


def mus_js_source() -> str:
    """The mus.js recorder source, for injection into the page under test."""
    return MUS_JS.read_text(encoding="utf-8")


def _ensure_paths() -> None:
    if str(CT_SRC) not in sys.path:
        sys.path.insert(0, str(CT_SRC))
    # featurizer/feature_index read features/*.txt relative to PROJECT_ROOT.
    os.environ.setdefault("PROJECT_ROOT", str(CT))


def _normalize_frames(frames: list) -> list:
    """Map mus.js frames to the event tuples BehavioralFV expects."""
    from classifier_training.data_preprocessing import preprocess_tuple  # type: ignore

    normalized = []
    for ev in frames:
        if not ev:
            continue
        et = ev[0]
        if et == "m":
            normalized.append(["mm"] + list(ev[1:]))
        elif et == "s":
            continue
        else:
            normalized.append(list(ev))
    events = []
    for e in normalized:
        try:
            events.append(list(preprocess_tuple(tuple(e))))
        except Exception:
            continue
    return events


def fp_agent_verdict(frames: list, *, model_name: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Run the real fp-agent classifier. Returns a structured verdict dict.

    status is one of: ok, no_events, error. On error/no_events we report honestly
    rather than fabricating a verdict (no silent fallback).
    """
    n_moves = sum(1 for f in frames if f and f[0] == "m")
    try:
        _ensure_paths()
        from classifier_training.featurizer import BehavioralFV  # type: ignore
        from classifier_training.common import load_model  # type: ignore
        import numpy as np  # type: ignore

        events = _normalize_frames(frames)
        if not events:
            return {"status": "no_events", "n_mouse_frames": n_moves,
                    "note": "no usable behavioral events after normalization"}

        fv = BehavioralFV()
        fv.parse_events(events)
        vec = fv.extract_feature_vector()

        model_path = CT / "classifiers" / model_name
        if not model_path.is_file():
            return {"status": "error", "note": f"model not found: {model_path}"}

        model = load_model(str(model_path))
        X = np.asarray([vec], dtype=float)
        pred_idx = int(model.predict(X)[0])
        try:
            proba = [float(p) for p in model.predict_proba(X)[0]]
        except Exception:
            proba = []

        n = len(proba)
        if n == len(ALL_CLASSES_LABELS):
            labels = ALL_CLASSES_LABELS
        elif n == len(BROWSING_ONLY_LABELS):
            labels = BROWSING_ONLY_LABELS
        else:
            labels = [f"class-{i}" for i in range(max(n, pred_idx + 1))]

        label = labels[pred_idx] if 0 <= pred_idx < len(labels) else f"class-{pred_idx}"
        human_p = proba[labels.index("Human")] if ("Human" in labels and proba) else None

        return {
            "status": "ok",
            "model": model_name,
            "predicted_label": label,
            "verdict": "HUMAN" if label == "Human" else "BOT",
            "confidence": round(max(proba), 4) if proba else None,
            "human_probability": round(human_p, 4) if human_p is not None else None,
            "labels": labels,
            "proba": [round(p, 4) for p in proba],
            "behavioral_vec_len": len(vec),
            "n_mouse_frames": n_moves,
        }
    except Exception as exc:  # noqa: BLE001 - report honestly
        return {"status": "error", "note": f"{type(exc).__name__}: {str(exc)[:300]}",
                "n_mouse_frames": n_moves}


if __name__ == "__main__":
    # Smoke test on synthetic frames: a rigid straight bot trace vs a wiggly one.
    import json
    import math
    import random

    rigid = []
    t, x, y = 1000.0, 100.0, 150.0
    for _ in range(120):
        x += 2.8; y += 0.1; t += 16.0
        rigid.append(["m", x, y, t])

    rng = random.Random(7)
    wiggly = []
    t, x, y = 1000.0, 120.0, 160.0
    for _ in range(120):
        x += rng.uniform(1.5, 4.5); y += rng.uniform(-1.2, 1.8)
        t += rng.choice([8.0, 9.0, 12.0, 22.0, 31.0])
        wiggly.append(["m", x, y, t])
        if rng.random() < 0.35:
            x -= rng.uniform(0.8, 2.5); t += rng.uniform(4, 11)
            wiggly.append(["m", x, y, t])

    for name, fr in (("rigid", rigid), ("wiggly", wiggly)):
        print(name, "->", json.dumps(fp_agent_verdict(fr)))
