#!/usr/bin/env python3
"""List trustworthy paths for an exact PR base/head range, or fail open.

An Actions merge checkout normally contains both PR parents. Prefer Git's
status-bearing diff so the compare API's 300-file limit cannot hide changes.
The API is a fallback only when its entire file list is demonstrably present.
Removed/renamed content is conservatively full-selection: the classifier
reads the current checkout and cannot inspect the old marker of a deleted test.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.ci.push_changed_files import COMPARE_FILE_CAP, compare_via_gh

_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


class FailOpen(Exception):
    """The exact PR change set is unavailable or unsafe to narrow."""


def _local_diff(base: str, head: str, root: Path) -> list[str] | None:
    for sha in (base, head):
        found = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if found.returncode:
            return None
    result = subprocess.run(
        ["git", "diff", "--name-status", "-z", "--find-renames", f"{base}...{head}", "--"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return None
    fields = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    if fields[-1:] == [""]:
        fields.pop()
    paths: list[str] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        i += 1
        count = 2 if status.startswith(("R", "C")) else 1
        if i + count > len(fields):
            raise FailOpen("incomplete local git diff")
        changed = fields[i : i + count]
        i += count
        if status.startswith(("D", "R", "C")) or not status.startswith(("A", "M", "T")):
            raise FailOpen(f"{status} needs former content or wider coverage")
        paths.extend(changed)
    return paths


def changed_files(
    base: str,
    head: str,
    repo: str,
    root: Path,
    *,
    compare: Callable[[str, str, str], dict[str, Any]] = compare_via_gh,
) -> list[str]:
    """Use the exact SHA range; raise FailOpen if a narrow answer is unsafe."""
    if not _SHA.fullmatch(base) or not _SHA.fullmatch(head) or not repo or base == head:
        raise FailOpen("missing, invalid or identical event SHAs")
    try:
        paths = _local_diff(base, head, root)
    except OSError:
        paths = None
    if paths is None:
        try:
            payload = compare(repo, base, head)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            raise FailOpen(f"compare API unavailable: {exc}") from exc
        if payload.get("status") not in {"ahead", "diverged"}:
            raise FailOpen("compare range is not ahead or diverged")
        raw = payload.get("files")
        if not isinstance(raw, list) or len(raw) >= COMPARE_FILE_CAP:
            raise FailOpen("compare file list missing or at the 300-file cap")
        paths = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise FailOpen("invalid compare file entry")
            status = entry.get("status")
            if status not in {"added", "modified", "changed"}:
                raise FailOpen(f"compare status {status!r} needs wider coverage")
            if entry.get("previous_filename"):
                raise FailOpen("renamed compare entry needs former content")
            paths.append(entry.get("filename"))
    if not paths or any(
        not isinstance(p, str) or not p or p != p.strip() or "\n" in p or "\r" in p
        for p in paths
    ):
        raise FailOpen("empty or unrepresentable changed path list")
    return sorted(set(paths))


def main() -> int:
    env = os.environ
    try:
        files = changed_files(
            env.get("BASE_SHA", ""),
            env.get("HEAD_SHA", ""),
            env.get("REPO", ""),
            Path.cwd(),
        )
    except FailOpen as reason:
        print(f"::warning::pr_changed_files: {reason}; all lanes run", file=sys.stderr)
        return 0
    print("\n".join(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
