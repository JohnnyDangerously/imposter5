# Real-Hardware Run — Red (laptop, headed) vs Blue (AWS, HTTPS)

Date: 2026-06-06

## Topology (authentic, no OS bleed)
- **Red:** runs natively on the laptop in a **headed** browser — real Apple GPU,
  real OS fonts/timezone, real channel build. New `--engine native` launches the
  actual installed Google Chrome; `--engine cloak --no-headless` runs the cloak
  stealth Chromium headed.
- **Blue:** deployed to **AWS EC2** (t3.small, AL2023, us-east-1,
  acct 049706517866, instance `i-0dd9244a48582d1b8`, `100.28.223.80`) in Docker,
  served over **HTTPS** (uvicorn TLS, self-signed cert `CN=100.28.223.80`).
  Security group allows 22/443 from the laptop IP only. Red reaches it over a
  real network path with a real TLS handshake (real JA3 now available to Blue).

## Results

| Run | Red engine (laptop, headed) | L2 env | L3 behav | L4 honeypot | Verdict | FP agent |
|-----|-----------------------------|--------|----------|-------------|---------|----------|
| Native baseline | real Chrome, no cloak | **50** | 75 | 100 | BOT_DETECTED 10% | Human 0.99 |
| **Flagship** | cloak (humanize) | **100** | 75 | 100 | SUSPICIOUS 75% | Human 0.76 |

Compare to the earlier **headless local sandbox** numbers:
- raw Playwright headless → L2 **25** (leaks: `navigator.webdriver` **+ SwiftShader
  `server_gpu_detected` high**).
- native headed real Chrome → L2 **50** (leak: `navigator.webdriver` only).

## Conclusions

### 1. The GPU + headless leaks were sandbox artifacts — real hardware removes them.
Going headless→headed on real hardware, Blue's Layer 2 `hardware:gpu` and
`automation:headless` checks flip from leak to **pass**. The high-severity
SwiftShader `server_gpu_detected` tell disappears entirely. This confirms the
authenticity caveat from the first full run: those signals were measuring the
test rig, not the client.

### 2. The only environmental tell left for un-cloaked automation is `navigator.webdriver`.
Real headed Chrome driven by Playwright still exposes `navigator.webdriver=true`
(critical) — the canonical CDP/automation marker. That single leak is what drops
the native baseline to BOT_DETECTED.

### 3. cloak closes it — on real hardware Red is environmentally invisible.
With cloak headed on the laptop, **L2 = 100 and L4 = 100**: webdriver patched,
real GPU, real OS, all honeypots evaded. Environment and application layers give
Blue nothing.

### 4. The last remaining signal is the controlled-script artifact, not the bot.
The flagship run's only leak is L3 `deterministic_transitions` — the **fixed
controlled action order**, identical to the local runs. It is a property of the
harness script, not of Red's motion or environment. The positive control
(`backend/l3_control.py`) shows a varied/non-deterministic action order scores
L3 = 100 (HUMAN_EVADED). Markov mode's high-level variety currently collapses in
Blue's observable alphabet (`mousemove↔idle`); raising Red's *observable* event
variety is the one remaining step to a clean HUMAN verdict.

### Net
On authentic real hardware over a real network, a cloak-humanized Red is
environmentally and behaviorally indistinguishable from a human **except** for
the harness's scripted action ordering. The battleground that remains for Blue is
(a) the `navigator.webdriver`/CDP marker for un-cloaked clients, and (b)
network-layer signals (TLS/JA3/IP) now that a real HTTPS path exists — not mouse
biometrics, which Red has beaten on the merits.

## Reproduce
```
# Blue: https://100.28.223.80/sandbox  (EC2, TLS)
# Red on laptop, headed:
backend/.venv/bin/python -m harness.redblue_runner \
  --target-url https://100.28.223.80/sandbox \
  --persona focused_power_user --engine {native|cloak} \
  --mode {controlled|markov} --no-headless \
  --out harness/out/<run>.json
```

## AWS teardown (when done)
```
aws ec2 terminate-instances --instance-ids i-0dd9244a48582d1b8 --region us-east-1
aws ec2 delete-security-group --group-id sg-053a75f9749db66cf --region us-east-1
aws ec2 delete-key-pair --key-name lhhl-blue --region us-east-1
```
