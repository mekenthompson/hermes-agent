#!/usr/bin/env python3
"""Fail closed unless the newest matching main CI run for this SHA succeeded.

With ``--wait`` the gate polls until that newest run has completed (or the
timeout elapses, which also fails closed) before applying the same rule.
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from collections.abc import Callable, Sequence
from typing import Any

def latest_exact_main_ci_run(runs: Sequence[dict[str, Any]], sha: str, workflow_path: str) -> dict[str, Any] | None:
    matching = [run for run in runs if run.get("event") == "push" and run.get("head_sha") == sha and run.get("head_branch") == "main" and run.get("path") == workflow_path]
    if not matching:
        return None
    return max(matching, key=lambda run: (str(run.get("created_at", "")), int(run.get("id", 0))))

def latest_exact_main_ci_is_green(runs: Sequence[dict[str, Any]], sha: str, workflow_path: str) -> bool:
    latest = latest_exact_main_ci_run(runs, sha, workflow_path)
    return latest is not None and latest.get("status") == "completed" and latest.get("conclusion") == "success"

def wait_for_exact_main_ci(
    fetch: Callable[[], Sequence[dict[str, Any]]],
    sha: str,
    workflow_path: str,
    *,
    timeout_seconds: float,
    interval_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = time.sleep,
    log: Callable[[str], Any] = lambda message: print(message, file=sys.stderr),
) -> bool:
    """Poll until the latest exact-SHA main run completes; True only on success.

    A run that has not been created yet, or is still queued/in progress, keeps
    polling. A newer run for the same SHA supersedes an older one on every
    poll, so the verdict is always the latest run's. Timing out returns False.
    """
    started = clock()
    while True:
        latest = latest_exact_main_ci_run(fetch(), sha, workflow_path)
        if latest is not None and latest.get("status") == "completed":
            return latest.get("conclusion") == "success"
        elapsed = clock() - started
        state = "absent" if latest is None else str(latest.get("status"))
        if elapsed >= timeout_seconds:
            log(f"exact-SHA main CI run still {state} after {elapsed:.0f}s; giving up")
            return False
        log(f"exact-SHA main CI run {state}; waiting {interval_seconds:.0f}s ({elapsed:.0f}s elapsed)")
        sleep(interval_seconds)

def fetch_runs(repository: str, workflow: str, sha: str, call: Callable[..., Any] = subprocess.run) -> list[dict[str, Any]]:
    result = call(["gh", "api", f"repos/{repository}/actions/workflows/{workflow}/runs?event=push&head_sha={sha}&per_page=100"], text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "GitHub Actions API request failed")
    payload = json.loads(result.stdout)
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise RuntimeError("GitHub Actions API response lacks workflow_runs")
    return runs

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True); parser.add_argument("--workflow", required=True)
    parser.add_argument("--sha", required=True); parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--wait", action="store_true", help="poll until the latest exact-SHA main run completes")
    parser.add_argument("--timeout-minutes", type=float, default=30.0)
    parser.add_argument("--interval-seconds", type=float, default=20.0)
    args = parser.parse_args()
    if args.wait:
        green = wait_for_exact_main_ci(
            lambda: fetch_runs(args.repository, args.workflow, args.sha),
            args.sha,
            args.workflow_path,
            timeout_seconds=args.timeout_minutes * 60,
            interval_seconds=args.interval_seconds,
        )
    else:
        green = latest_exact_main_ci_is_green(fetch_runs(args.repository, args.workflow, args.sha), args.sha, args.workflow_path)
    if not green:
        raise SystemExit("latest exact-SHA main CI run is absent, incomplete, or not successful")
    return 0
if __name__ == "__main__": raise SystemExit(main())
