"""Local (no-Docker) Red-vs-Blue orchestrator.

Starts the Blue (last-human-line) detection server as a subprocess, waits for it
to become healthy, runs one or more real Red matchups against it via
``redblue_runner``, and writes a combined matchup report. Use this for Phase A
measurement on a single machine; use ``docker-compose.yml`` for namespace-isolated
runs (see RUNBOOK.md).

Assumes the two repos are siblings:  ~/repos/imposter5  and  ~/repos/last-human-line
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
RED_REPO = HERE.parent
DEFAULT_BLUE = RED_REPO.parent / "last-human-line"


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/system/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def start_blue(blue_repo: pathlib.Path, host: str, port: int) -> subprocess.Popen | None:
    """Launch Blue's uvicorn unless something is already serving on the port."""
    base = f"http://{host}:{port}"
    if _healthy(base):
        print(f"[orchestrate] Blue already healthy at {base}; reusing it")
        return None

    py = blue_repo / "backend" / ".venv" / "bin" / "python"
    py = py if py.exists() else pathlib.Path(sys.executable)
    log = (HERE / "out" / "blue.log").open("w", encoding="utf-8")
    (HERE / "out").mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(py), "-m", "uvicorn", "app:app", "--host", host, "--port", str(port)],
        cwd=str(blue_repo / "backend"),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if _healthy(base):
            print(f"[orchestrate] Blue healthy at {base} (pid {proc.pid})")
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"Blue exited early (code {proc.returncode}); see {HERE/'out'/'blue.log'}")
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("Blue did not become healthy within 30s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local Red-vs-Blue matchups (no Docker).")
    parser.add_argument("--blue-repo", default=str(DEFAULT_BLUE))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5190)
    parser.add_argument("--engine", default="auto", choices=["auto", "cloak", "playwright"])
    parser.add_argument("--personas", default="focused_power_user,curious_reader",
                        help="comma-separated persona names to run as separate matchups")
    args = parser.parse_args(argv)

    base = f"http://{args.host}:{args.port}"
    blue_proc = start_blue(pathlib.Path(args.blue_repo), args.host, args.port)
    out_dir = HERE / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        from harness.redblue_runner import run_matchup

        for persona in [p.strip() for p in args.personas.split(",") if p.strip()]:
            print(f"[orchestrate] running matchup persona={persona} engine={args.engine} ...")
            res = run_matchup(
                target_url=f"{base}/sandbox",
                persona=persona,
                engine=args.engine,
                headless=True,
            )
            (out_dir / f"matchup-{persona}.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
            results.append(res)
            b = res["blue_report"]
            print(f"[orchestrate]   -> Blue {b.get('evasion_score')}% / {b.get('verdict')} "
                  f"(critical leaks {b.get('critical_leak_count')})")
    finally:
        if blue_proc is not None:
            blue_proc.terminate()

    combined = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "blue_base": base,
        "matchups": results,
    }
    (out_dir / "matchups-combined.json").write_text(json.dumps(combined, indent=2), encoding="utf-8")
    print(f"[orchestrate] combined report -> {out_dir/'matchups-combined.json'}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(RED_REPO))
    raise SystemExit(main())
