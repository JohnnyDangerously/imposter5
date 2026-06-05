#!/usr/bin/env python3
"""
Standalone: given a frames JSON produced by the redteam detector (or any mus.js "frames" list
captured from the fp-agent honey site), run fp-agent's *actual* trained behavioral classifier
and print their tool's verdict + confidence.

This is what you hand to the security friend along with a captured frames file.

Usage (from inside fp-agent/classifier_training, or with PROJECT_ROOT set):
  uv run --with xgboost,scikit-learn,matplotlib,shap,orjson,psycopg2-binary,python-dotenv,numpy \
    python /path/to/fp-agent/get_fp_agent_real_verdict.py /tmp/fp_redteam_frames_....json

It will:
- Set PROJECT_ROOT if needed so features/*.txt are found.
- Normalize the mus "m"/"s" events to their "mm" etc. format.
- Run their exact BehavioralFV.extract_feature_vector().
- Load the behavioral_fingerprint_browsing_agents_only model (the one trained only on the known automation "FP agents").
- Predict and map the integer to a human label (Browser Use, Skyvern, etc.).
- Print the class + confidence.

This is *not* a heuristic. This is their XGBoost saying "given the behavioral features we engineered,
this session looks most like <label> from the data we collected by driving <these FP agents> against our honey."

The "FP agents" are the ones in data_collection/ (Skyvern, browser-use, ChatGPT Atlas / Comet / Claude computer-use wrappers, etc.).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Add classifier_training/src to sys.path so we can import from it
FP_AGENT_ROOT = os.path.dirname(os.path.abspath(__file__))
CLASSIFIER_TRAINING_SRC = os.path.join(FP_AGENT_ROOT, "classifier_training", "src")
if CLASSIFIER_TRAINING_SRC not in sys.path:
    sys.path.insert(0, CLASSIFIER_TRAINING_SRC)

# The sorted class order for the "browsing_agents_only" (Human removed) models.
# This matches what their AgentClassificationDataset.create_label_mapping does on sorted keys.
BROWSING_AGENTS_ONLY_LABELS: list[str] = [
    "Atlas Agent",
    "Browser Use",
    "ChatGPT Agent",
    "Claude",
    "Comet",
    "Manus",
    "Skyvern",
]


def normalize_mus_frames_to_events(raw_frames: list[list[Any]]) -> list[list[Any]]:
    """Convert the raw list from mus.js.getData().frames (or our saved {"frames": [...]})
    into the list of events their BehavioralFV.parse_events expects after their preprocess.
    """
    from classifier_training.data_preprocessing import preprocess_tuple  # type: ignore

    normalized: list[list[Any]] = []
    for ev in raw_frames:
        if not ev:
            continue
        et = ev[0]
        if et == "m":
            # mus move is 4-tuple; their "mm" expects 5 after inserting element_id "N/A"
            normalized.append(["mm"] + list(ev[1:]))
        elif et == "s":
            # mus start/ init marker; ignore for pure behavioral movement features
            continue
        else:
            normalized.append(list(ev))

    events: list[list[Any]] = []
    for e in normalized:
        try:
            events.append(list(preprocess_tuple(tuple(e))))
        except Exception:
            continue
    return events


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python get_fp_agent_real_verdict.py /path/to/frames.json")
        sys.exit(2)

    frames_path = sys.argv[1]
    with open(frames_path) as f:
        data = json.load(f)

    # Accept either the raw list or our {"mode": , "frames": } wrapper
    if isinstance(data, dict) and "frames" in data:
        raw_frames = data["frames"]
        mode = data.get("mode", "unknown")
    elif isinstance(data, list):
        raw_frames = data
        mode = "raw-list"
    else:
        print("Unrecognized frames file format")
        sys.exit(1)

    # Make sure PROJECT_ROOT lets feature_index find the features/ lists
    if not os.environ.get("PROJECT_ROOT"):
        # Prefer the classifier_training subdir if we can find it relative to this script
        here = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(here, "classifier_training")
        if os.path.isdir(os.path.join(candidate, "features")):
            os.environ["PROJECT_ROOT"] = candidate
        elif os.path.isdir(os.path.join(here, "features")):
            os.environ["PROJECT_ROOT"] = here
        else:
            # last resort: current dir
            os.environ["PROJECT_ROOT"] = os.getcwd()

    print("PROJECT_ROOT =", os.environ["PROJECT_ROOT"])

    # These imports must happen after PROJECT_ROOT is set (they do top-level reads of features/)
    from classifier_training.featurizer import BehavioralFV  # type: ignore
    from classifier_training.common import load_model  # type: ignore

    events = normalize_mus_frames_to_events(raw_frames)
    print(f"Normalized {len(events)} behavioral events from {len(raw_frames)} raw mus frames (mode={mode})")

    fv = BehavioralFV()
    fv.parse_events(events)
    behavioral_vec = fv.extract_feature_vector()
    print("Extracted behavioral feature vector, len =", len(behavioral_vec))

    model_file = os.path.join(
        os.environ["PROJECT_ROOT"], "classifiers", "behavioral_fingerprint_browsing_agents_only.json"
    )
    if not os.path.isfile(model_file):
        print("ERROR: expected model not found at", model_file)
        print("The fp-agent clone must have the classifiers/ JSONs (they are committed).")
        sys.exit(1)

    model = load_model(model_file)
    import numpy as np  # type: ignore

    X = np.asarray([behavioral_vec], dtype=float)
    pred_idx = int(model.predict(X)[0])
    proba = model.predict_proba(X)[0].tolist() if hasattr(model, "predict_proba") else None

    label = BROWSING_AGENTS_ONLY_LABELS[pred_idx] if 0 <= pred_idx < len(BROWSING_AGENTS_ONLY_LABELS) else f"unknown-{pred_idx}"
    conf = max(proba) if proba else None

    print("\n=== FP-AGENT REAL VERDICT ===")
    print(f"Input frames: {frames_path}")
    print(f"Model: behavioral_fingerprint_browsing_agents_only (trained only on the automation agents, no Human)")
    print(f"Predicted label: {label}")
    if conf is not None:
        print(f"Confidence: {conf:.4f}")
    if proba:
        print("Probs (Atlas Agent, Browser Use, ChatGPT Agent, Claude, Comet, Manus, Skyvern):")
        print(" ", [round(p, 4) for p in proba])

    print("\nInterpretation for redteam:")
    print("  If this says one of the known FP agents (Browser Use, Skyvern, etc.) with high confidence,")
    print("  their detector is clustering the captured behavior with that automation tool.")
    print("  'Human' would only appear if you used one of the all-classes models and it thought it looked human.")
    print("  (For all-classes you would also need to capture the fingerprint /fp POSTs to build the full combined vector.)")

    # Also dump the raw integer for scripting
    print("\nRAW: index=", pred_idx, "label=", label, "conf=", conf)


if __name__ == "__main__":
    main()
