#!/usr/bin/env bash
# Longitudinal equilibrium: N different new-Red sessions under ONE account identity.
# Tests Blue's cross-session clustering vs Red's per-session variability/drift.
set -u
cd /Users/john/repos/imposter5
PY=backend/.venv/bin/python
ID="acct_alpha"
URL="http://127.0.0.1:5190/sandbox?identity=${ID}"
N="${1:-4}"
echo "[longitudinal] $N sessions, identity=$ID"
for i in $(seq 1 "$N"); do
  echo "=== session $i/$N ==="
  BLUE_BASE_URL="http://127.0.0.1:5190" "$PY" harness/redblue_runner.py \
    --mode markov --engine cloak --persona focused_power_user \
    --target-url "$URL" --out "harness/out/longi_${i}.json" 2>&1 | tail -3
done
echo "[longitudinal] done"
