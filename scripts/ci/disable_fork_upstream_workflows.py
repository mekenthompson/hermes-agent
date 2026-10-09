#!/usr/bin/env python3
"""Disable upstream-only workflows on the maintained fork.

Deleting the files fights the next upstream sync: GitHub treats a restored
file as a new active workflow. Disabling by filename survives content updates.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = "mekenthompson/hermes-agent"
LIST = Path(__file__).with_name("fork_disabled_workflows.txt")


def disabled_names() -> list[str]:
    names = []
    for line in LIST.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def main() -> int:
    if "--list" in sys.argv[1:]:
        print("\n".join(disabled_names()))
        return 0
    failed = []
    for name in disabled_names():
        result = subprocess.run(
            ["gh", "workflow", "disable", name, "--repo", REPO],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            continue
        detail = (result.stderr or result.stdout or "").lower()
        if "not active" in detail or "already disabled" in detail:
            continue
        failed.append(name)
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
