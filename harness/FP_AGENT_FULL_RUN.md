# Full End-to-End Run — Real FP Agent vs Blue Statistical Layers

Date: 2026-06-06
Harness: `harness/redblue_runner.py` (Red `imposter5` driving Blue `last-human-line`'s
`/sandbox` on `127.0.0.1:5190`). Two behavioral detectors run on the **same session**:

- **Blue (statistical L2/L3/L4):** environment integrity (L2), hand-rolled
  behavioral biometrics — Shannon transition entropy, LLR, velocity/accel/jerk
  kinetics (L3), stateful multi-trap honeypots (L4). Penalty-based aggregation.
- **FP agent (real XGBoost):** the security partner's trained
  `behavioral_fingerprint_all_classes` model (8 classes incl. `Human`), fed the
  exact `BehavioralFV` vector built from `mus.js` frames captured live during the
  Red session. This is their tool's verdict, not a heuristic.

## Matrix

| Cell | Behavior | Engine | Blue L2 (env) | Blue L3 (behav) | Blue L4 (honeypot) | Blue verdict | FP agent (XGBoost) | mouse frames |
|------|----------|--------|---------------|-----------------|--------------------|--------------|--------------------|--------------|
| A | human-like (focused) | cloak (humanize) | 100 | 75 | 100 | **SUSPICIOUS** 75% | **Human** 0.76 | 7383 |
| B | robotic (naive) | cloak (humanize) | 100 | — | 100 | **SUSPICIOUS** 75% | **Human** 0.99 | 369 |
| C | human-like (focused) | raw Playwright | 25 | 75 | 100 | **BOT_DETECTED** 10% | **Human** 0.99 | 259 |
| D | robotic (naive) | raw Playwright | 25 | 100 | 100 | **BOT_DETECTED** 10% | **Human** 0.91 | 7 |

## Conclusions

### 1. Behavioral detection is blind here — on BOTH sides.
Neither detector ever flags a session on behavior:
- Blue's L3 stays **75–100** in every cell (high = looks human), including the
  teleporting naive bot (D: L3 = 100).
- The partner's **real XGBoost says `Human` in all four cells** (0.76–0.99),
  independent of cloak on/off and of human-like vs robotic behavior.

Behavioral biometrics from a mouse-frame stream cannot distinguish our
small-scale automation from a human. This confirms the standing thesis: at small
scale, behavior is not where the bot gets caught.

### 2. The only hard catch is the ENVIRONMENT (Layer 2), and cloak closes it.
The single discriminator is engine, not behavior:
- **Raw Playwright → L2 = 25 → BOT_DETECTED (10%).** Leak is
  `navigator.webdriver = true` (critical) + SwiftShader `server_gpu_detected`
  (high). honeypot_evaded was still True in C — the catch is environment, not L4.
- **cloak (humanize) → L2 = 100 → at worst SUSPICIOUS (75%).** cloak patches
  `navigator.webdriver` and the GPU renderer, so detection falls back to
  behavior, which (per #1) cannot finish the job.

So the whole detectability of small-scale red team collapses to fingerprint/
environment hygiene. With cloak, neither Blue's behavioral L3 nor the partner's
trained behavioral model can push past SUSPICIOUS.

### 3. The FP agent needs frame volume; teleporting starves it.
D (naive raw) produced only **7 mouse frames** (instantaneous moves) and the
model fell back to its prior (`Human` 0.91). A behavioral classifier is only
meaningful when the agent actually generates motion. A bot that teleports gives
the behavioral layer nothing — but it gets caught on environment instead (L2=25).

## What this means for next steps
- **Battleground is environment, not behavior.** To make Blue competitive against
  cloak, invest in environment/fingerprint signals (TLS/JA3, header order,
  client-hints, GPU/canvas/audio entropy, timing of automation hooks), not more
  behavioral math. The behavioral layers are already saturated.
- **Red is environment-bound too.** Drop cloak and you are caught instantly on
  `navigator.webdriver`. Red's entire stealth budget is the fingerprint patch;
  the humanized motion is, at this scale, unobserved by either detector.
- **Authenticity matters for L2.** These runs share the host's SwiftShader GPU
  (`server_gpu_detected`, high severity) — a sandbox artifact. The Docker/real-
  hardware isolation in `RUNBOOK.md` is needed before L2 numbers are trustworthy;
  a real GPU would remove that one high-severity leak and make the
  `navigator.webdriver` critical the sole raw-Playwright tell.

## Reproduce
```
# Blue up on :5190, then for each cell:
backend/.venv/bin/python -m harness.redblue_runner \
  --target-url http://127.0.0.1:5190/sandbox \
  --persona {focused_power_user|naive_bot} \
  --engine {cloak|playwright} --mode {controlled|markov} \
  --out harness/out/<cell>.json
```
Raw per-cell reports: `harness/out/fp-{A,B,C,D}-*.json`.
