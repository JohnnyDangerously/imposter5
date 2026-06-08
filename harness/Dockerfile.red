# Red team: imposter5 bot/evasion runner.
# Needs headless Chromium for Playwright + cloakbrowser, so we start from the
# official Playwright Python image (browsers + OS deps preinstalled).
# Build context = the imposter5 repo root (so backend/ and harness/ are visible).
#
# IMPORTANT (authenticity ceiling): inside a Linux container, headless Chromium
# renders via SwiftShader/llvmpipe and runs with headless flags. Blue's Layer 2
# environment checks will flag this as server-GPU / headless. This image fairly
# exercises the BEHAVIORAL layers; it is NOT a fair environment-evasion test.
# For that, run Red headed on real consumer hardware (see RUNBOOK.md, Phase B).
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Install the imposter5 backend project (pulls playwright, cloakbrowser, and the
# ML stack from backend/pyproject.toml). Editable install points imposter5 at
# src/imposter5 without a rebuild.
COPY backend/ /app/backend/
RUN pip install --no-cache-dir -e /app/backend

# Harness runner + its contract (redblue_runner.py).
COPY harness/ /app/harness/

# Make the imposter5 package (and sibling src packages, e.g. cookies) importable.
ENV PYTHONPATH=/app/backend/src

# Defaults; compose overrides BLUE_BASE_URL/PERSONA/ENGINE. The runner also reads
# these env vars as fallbacks for its CLI flags.
ENV BLUE_BASE_URL=http://blue:5190 \
    PERSONA=focused_power_user \
    ENGINE=auto

# `python -m harness.redblue_runner` resolves harness/ as a namespace package
# from /app (the WORKDIR). Shell form so the env vars expand at runtime.
# Red reaches Blue ONLY over real HTTP at ${BLUE_BASE_URL} — never in-process.
CMD ["sh", "-c", "python -m harness.redblue_runner --target-url ${BLUE_BASE_URL}/sandbox --persona ${PERSONA} --out /out/red.json --engine ${ENGINE}"]
