"""Small, testable policy helpers for the CI orchestrator workflow."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Mapping

FORK = "mekenthompson/hermes-agent"


@dataclass(frozen=True)
class NeedsSummary:
    """The compact job-result map and jobs that prevent a passing gate."""

    compact: dict[str, str]
    failed: list[str]


def should_cancel_in_progress(event_name: str, repository: str, ref: str) -> bool:
    """Supersede PRs everywhere and only main pushes in the maintained fork."""
    return event_name == "pull_request" or (
        event_name == "push"
        and repository == "mekenthompson/hermes-agent"
        and ref == "refs/heads/main"
    )


def selected_jobs(
    lanes: Mapping[str, str], repository: str, event_name: str
) -> dict[str, bool]:
    """One applicability decision consumed by both CI jobs and their gate."""

    def on(lane: str) -> bool:
        return lanes.get(lane) == "true"

    fork = repository == FORK
    pr = event_name == "pull_request"
    product = on("python_prod") or on("frontend")
    return {
        "detect": True,
        "tests": on("python"),
        "tests-os": on("python") and (not fork or on("os_tests")),
        "lint": on("python"),
        "js-tests": on("frontend"),
        "installer-tests": on("installer"),
        "rust-tests": on("rust"),
        "bootstrap-installer": on("bootstrap"),
        "e2e-desktop": False,  # existing intentional disablement
        "e2e-desktop-core": product,
        "e2e-desktop-update": product and (not fork or on("upgrade")),
        "docs-site": on("site") and not fork,
        "history-check": pr,
        "contributor-check": on("python") and (not fork or pr),
        "uv-lockfile": on("uv_lock"),
        "infographic-check": not fork or on("binary_artifacts"),
        "profile-artifact-check": not fork or on("binary_artifacts"),
        "icons-freshness-check": True,
        "case-collision-check": True,
        "lazy-deps-guard": True,
        "lockfile-diff": pr and on("npm_lock"),
        "docker-lint": on("docker_meta"),
        "supply-chain": pr and (on("scan") or on("deps")),
        "review-labels": pr and (on("ci_review") or on("mcp_catalog")),
        "osv-scanner": not fork or on("uv_lock") or on("npm_lock") or on("deps"),
    }


def evaluate_needs(
    needs: Mapping[str, Mapping[str, str]],
    selected: Mapping[str, bool],
    *,
    critical_findings: bool = False,
) -> NeedsSummary:
    """Every expected job must report; a selected one must succeed."""
    compact = {name: info.get("result", "missing") for name, info in needs.items()}
    if not selected or any(type(value) is not bool for value in selected.values()):
        return NeedsSummary(compact=compact, failed=["selected_jobs"])
    applicable = dict(selected)
    if critical_findings and applicable.get("review-labels") is False:
        applicable["review-labels"] = True
    failed = [
        name
        for name, required in applicable.items()
        if compact.get(name)
        not in ({"success"} if required else {"success", "skipped"})
    ]
    failed.extend(
        name
        for name, result in compact.items()
        if name not in applicable and result != "success"
    )
    return NeedsSummary(compact=compact, failed=failed)


def main() -> int:
    if "--select" in sys.argv[1:]:
        lanes = json.loads(os.environ.get("LANES", "{}"))
        selected = selected_jobs(
            lanes, os.environ.get("REPO", ""), os.environ.get("EVENT_NAME", "")
        )
        out = "selected_jobs=" + json.dumps(selected, separators=(",", ":"))
        print(out)
        if path := os.environ.get("GITHUB_OUTPUT"):
            with open(path, "a", encoding="utf-8") as output:
                output.write(out + "\n")
        return 0
    needs = json.load(sys.stdin)
    try:
        selected = json.loads(os.environ.get("SELECTED_JOBS", "{}"))
    except json.JSONDecodeError:
        selected = {}
    summary = evaluate_needs(
        needs, selected, critical_findings=os.environ.get("CRITICAL_FINDINGS") == "true"
    )
    needs_json = json.dumps(summary.compact, separators=(",", ":"))
    print(f"needs-json={needs_json}")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as output:
            output.write(f"needs-json={needs_json}\n")
    for name, result in sorted(summary.compact.items()):
        icon = "✅" if name not in summary.failed else "❌"
        print(f"{icon} {name}: {result}")
    if summary.failed:
        print(
            f"::error::{len(summary.failed)} job(s) did not pass: "
            f"{', '.join(summary.failed)}"
        )
        return 1
    print("All selected checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
