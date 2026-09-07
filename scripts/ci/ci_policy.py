"""Small, testable policy helpers for the CI orchestrator workflow."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Mapping


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


def evaluate_needs(needs: Mapping[str, Mapping[str, str]]) -> NeedsSummary:
    """Treat only successful and skipped prerequisites as passing."""
    compact = {name: info["result"] for name, info in needs.items()}
    failed = [
        name
        for name, result in compact.items()
        if result not in {"success", "skipped"}
    ]
    return NeedsSummary(compact=compact, failed=failed)


def main() -> int:
    needs = json.load(sys.stdin)
    summary = evaluate_needs(needs)
    needs_json = json.dumps(summary.compact, separators=(",", ":"))
    print(f"needs-json={needs_json}")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as output:
            output.write(f"needs-json={needs_json}\n")
    for name, result in sorted(summary.compact.items()):
        icon = "✅" if result in {"success", "skipped"} else "❌"
        print(f"{icon} {name}: {result}")
    if summary.failed:
        print(
            f"::error::{len(summary.failed)} job(s) did not pass: "
            f"{', '.join(summary.failed)}"
        )
        return 1
    print("All checks passed (or were skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
