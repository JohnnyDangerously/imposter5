# Red-vs-Blue Anti-Bot Harness — Runbook

This harness runs the **Red** team (imposter5 bot/evasion runner) against the
**Blue** team (last-human-line anti-bot detector). Red drives a real browser
through Blue's `/sandbox` page, the page streams behavioral telemetry to Blue's
`POST /api/lhhl/submit`, and Blue scores it. The runner writes a combined
Red+Blue JSON report.

Two ways to run:

- **Local (no Docker):** two processes on `127.0.0.1`. Fast inner loop.
- **Docker compose:** two isolated containers on a private bridge network.

> Red must always reach Blue over **real HTTP** — never in-process. That is what
> keeps the telemetry authentic (real navigation, real timing, real transport).

---

## 1. Run locally without Docker (two processes)

Prereqs: both repos already have a populated `backend/.venv` (Python 3.11).

### Terminal A — start Blue (detector)

```bash
cd /Users/john/repos/last-human-line/backend
./.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 5190
```

Sanity check (Terminal C):

```bash
curl -s http://127.0.0.1:5190/system/health
# -> {"success": true, "data": {"status": "healthy", ...}}
```

### Terminal B — run Red (bot) against Blue

```bash
cd /Users/john/repos/imposter5
backend/.venv/bin/python -m harness.redblue_runner \
  --target-url http://127.0.0.1:5190/sandbox \
  --persona focused_power_user \
  --out harness/out/red.json \
  --engine auto
```

Notes:

- `python -m harness.redblue_runner` must be invoked from the **imposter5 repo
  root** so `harness/` resolves as a package.
- The imposter5 package must be importable. The runner self-inserts
  `backend/src` onto `sys.path`; if you hit `ModuleNotFoundError: imposter5`,
  prefix the command with `PYTHONPATH=backend/src`.
- Add `--no-headless` to watch the browser drive the page; default is headless.
- Non-zero exit means the run failed (Blue unreachable, navigation error, etc.).

Read the report:

```bash
cat /Users/john/repos/imposter5/harness/out/red.json | python -m json.tool | less
```

---

## 2. Run with Docker compose (two isolated containers)

> Docker was not available when these files were authored — the commands below
> are ready to run once you have Docker.

From the imposter5 repo root:

```bash
cd /Users/john/repos/imposter5
docker compose -f harness/docker-compose.yml up --build
```

What happens:

1. `blue` builds from `../../last-human-line/Dockerfile`
   (`python:3.11-slim`, no browser) and serves `app:app` on `:5190`.
2. Compose waits for Blue's healthcheck (`GET /system/health`) to pass.
3. `red` builds from `harness/Dockerfile.red`
   (Playwright Python image, headless Chromium) and runs the runner against
   `http://blue:5190/sandbox`.
4. Red writes the combined report to `/out/red.json`, mounted to the host at
   `./harness/out/red.json`.

`red` is a one-shot job and exits when the run finishes. To tear everything down:

```bash
docker compose -f harness/docker-compose.yml down
```

Read the report on the host:

```bash
python -m json.tool harness/out/red.json | less
```

Override the persona / engine without editing files:

```bash
PERSONA=cautious_browser ENGINE=cloak \
  docker compose -f harness/docker-compose.yml up --build
```

(`docker-compose.yml` passes `PERSONA`/`ENGINE` straight into the `red`
container, where the runner reads them as flag fallbacks.)

Blue's `:5190` is published to the host only for inspection; Red does not use the
published port — it talks to `blue:5190` on the private `redblue_net` bridge.

---

## 3. Isolation & authenticity

### What the containers give you

- **No cross-wiring.** Red and Blue are separate images with separate
  filesystem and network namespaces. They share no Python process, no imported
  module, and no in-memory signatures. Blue only ever sees what arrives on the
  wire.
- **Real-HTTP telemetry.** Red drives a real headless browser through Blue's
  `/sandbox`; the page emits genuine behavioral telemetry to
  `POST /api/lhhl/submit`. This is a faithful test of the **behavioral** layers
  (mouse/scroll/typing kinematics, Markov state transitions, timing).

### The authenticity ceiling (important)

A headless Chromium running inside a **Linux container** renders through
**SwiftShader / llvmpipe** (software GL) and runs with headless flags. Blue's
**Layer 2 (environment)** checks flag exactly this signature as
server-GPU / headless. So:

- The container path is a **fair behavioral test** but **NOT a fair
  environment-evasion test**. Expect Layer 2 to detect the container regardless
  of how good Red's behavior is.
- Do not conclude "Red was caught" from a containerized run without separating
  the behavioral score from the environment score in `red.json`.

### Phase B — fair environment test

To test environment evasion honestly:

1. Run **Red headed on real consumer hardware** (a real GPU + display, not a
   container, not a cloud Linux box). Drop `--no-headless`/headless flags so
   Chromium uses the real graphics stack.
2. Run **Blue on a separate host and domain** so Red crosses a real network
   boundary to a neutral endpoint.
3. Optionally route Red through a **residential proxy**. cloakbrowser reads the
   proxy from the `AUTOMATION_CONNECTOR_PROXY` env var, e.g.:

   ```bash
   AUTOMATION_CONNECTOR_PROXY="http://user:pass@residential-host:port" \
     backend/.venv/bin/python -m harness.redblue_runner \
       --target-url https://blue.example.com/sandbox \
       --engine cloak --no-headless \
       --out harness/out/red-phaseB.json
   ```

4. Keep the rule intact: Red reaches Blue over **real HTTP** (never in-process)
   so transport, TLS, and timing telemetry stay authentic.

---

## 4. Phase B / AWS note

AWS is worth it for **Blue**, not for **Red**:

- **Blue on AWS:** good. It is a neutral, public, always-on endpoint on its own
  domain — exactly what you want Red to cross a real network to reach, and it
  scales to many concurrent runs.
- **Red on AWS:** not worth it for environment authenticity. Cloud Linux
  instances (even "GPU" ones) still render headless Chromium through
  SwiftShader/llvmpipe-style software paths and trip Blue's Layer 2 the same way
  a container does. Use **real consumer hardware** for any Red run where
  environment authenticity matters; reserve cloud only for behavioral scale.
