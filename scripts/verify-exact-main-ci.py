#!/usr/bin/env python3
"""Require successful CI proof from the newest main push run for this SHA.

By default the whole run must pass. ``--required-job`` instead waits for one
aggregate in its current attempt, allowing unrelated CI to finish separately.
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

def named_job_verdict(run: dict[str, Any], jobs: Sequence[dict[str, Any]], name: str) -> bool | None:
    """None means pending; missing/ambiguous/stale proof can never pass."""
    if run.get("status") == "completed" and run.get("conclusion") not in {"success", "failure"}:
        return False
    matching = [job for job in jobs if job.get("name") == name]
    if not matching:
        return False if run.get("status") == "completed" else None
    if len(matching) != 1:
        return False
    job = matching[0]
    if any(job.get(key) != run.get(source) for key, source in (
        ("run_id", "id"), ("run_attempt", "run_attempt"), ("head_sha", "head_sha"),
    )):
        return False
    if job.get("status") == "completed":
        return job.get("conclusion") == "success"
    return False if run.get("status") == "completed" else None

def wait_for_exact_main_ci(
    fetch: Callable[[], Sequence[dict[str, Any]]],
    sha: str,
    workflow_path: str,
    *,
    timeout_seconds: float,
    interval_seconds: float,
    job_name: str | None = None,
    fetch_jobs: Callable[[dict[str, Any]], Sequence[dict[str, Any]]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = time.sleep,
    log: Callable[[str], Any] = lambda message: print(message, file=sys.stderr),
) -> bool:
    """Poll until the latest exact-SHA main run completes; True only on success.

    A run that has not been created yet, or is still queued/in progress, keeps
    polling. A newer run for the same SHA supersedes an older one on every
    poll, so the verdict is always the latest run's. Timing out returns False.
    """
    if job_name is not None and fetch_jobs is None:
        raise ValueError("named-job verification requires a job fetcher")
    started = clock()
    while True:
        latest = latest_exact_main_ci_run(fetch(), sha, workflow_path)
        if latest is not None and job_name is not None:
            verdict = named_job_verdict(latest, fetch_jobs(latest), job_name)
            if verdict is False:
                return False
            if verdict is True:
                # A rerun can start during the jobs API request. Never accept
                # the previous attempt's successful gate as its replacement.
                current = latest_exact_main_ci_run(fetch(), sha, workflow_path)
                if current is not None and all(current.get(key) == latest.get(key) for key in ("id", "run_attempt")):
                    return current.get("status") != "completed" or current.get("conclusion") in {"success", "failure"}
        elif latest is not None and latest.get("status") == "completed":
            return latest.get("conclusion") == "success"
        elapsed = clock() - started
        state = "absent" if latest is None else str(latest.get("status"))
        if elapsed >= timeout_seconds:
            log(f"exact-SHA main CI run still {state} after {elapsed:.0f}s; giving up")
            return False
        log(f"exact-SHA main CI run {state}; waiting {interval_seconds:.0f}s ({elapsed:.0f}s elapsed)")
        sleep(min(interval_seconds, timeout_seconds - elapsed))

def fetch_run_jobs(repository: str, run: dict[str, Any], call: Callable[..., Any] = subprocess.run) -> list[dict[str, Any]]:
    run_id, attempt = run.get("id"), run.get("run_attempt")
    if any(type(value) is not int or value < 1 for value in (run_id, attempt)):
        raise RuntimeError("GitHub Actions run lacks a valid id/attempt")
    result = call([
        "gh", "api", "--paginate", "--slurp",
        f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
    ], text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "GitHub Actions jobs API request failed")
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or not all(
        isinstance(page, dict) and isinstance(page.get("jobs"), list)
        and all(isinstance(job, dict) for job in page["jobs"]) for page in pages
    ):
        raise RuntimeError("GitHub Actions API response lacks jobs")
    return [job for page in pages for job in page["jobs"]]

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
    parser.add_argument("--required-job", help="require this aggregate job in the latest run attempt instead of the entire workflow")
    parser.add_argument("--timeout-minutes", type=float, default=30.0)
    parser.add_argument("--interval-seconds", type=float, default=20.0)
    args = parser.parse_args()
    if args.wait or args.required_job:
        green = wait_for_exact_main_ci(
            lambda: fetch_runs(args.repository, args.workflow, args.sha),
            args.sha,
            args.workflow_path,
            timeout_seconds=args.timeout_minutes * 60 if args.wait else 0,
            interval_seconds=args.interval_seconds,
            job_name=args.required_job,
            fetch_jobs=lambda run: fetch_run_jobs(args.repository, run),
        )
    else:
        green = latest_exact_main_ci_is_green(fetch_runs(args.repository, args.workflow, args.sha), args.sha, args.workflow_path)
    if not green:
        raise SystemExit("latest exact-SHA main CI proof is absent, incomplete, or not successful")
    return 0
if __name__ == "__main__": raise SystemExit(main())
