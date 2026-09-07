#!/usr/bin/env python3
"""Fork-only: list the files a push to ``main`` changed, or nothing to fail open.

``detect-changes`` runs this on ``push`` events in the maintained fork so the
classifier can scope post-merge validation to the pushed range instead of
running every lane. Upstream never calls it: its pushes stay fail-open.

Prints one repo-relative path per line. Prints NOTHING (and a ``::warning::``
on stderr) whenever the range cannot be trusted, which makes the classifier
fail open exactly as it does for an empty PR diff:

* ``before`` is missing or all zeros (branch creation);
* the push was forced (``github.event.forced``);
* the compare API reports the range is not a fast-forward (``before`` is not
  an ancestor of ``after``);
* the compare is truncated at the API's 300-file cap, so lanes could hide in
  the files it did not list;
* the API fails after a retry.

Environment: ``REPO``, ``PUSH_BEFORE``, ``PUSH_AFTER``, ``PUSH_FORCED``,
``GH_TOKEN`` (for ``gh api``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

# The compare endpoint lists at most 300 files however many pages are read.
COMPARE_FILE_CAP = 300
_ZERO_SHA = "0" * 40


class FailOpen(Exception):
    """The pushed range cannot be trusted; run every lane."""


def compare_via_gh(repo: str, before: str, after: str, call: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    result = call(
        ["gh", "api", f"repos/{repo}/compare/{before}...{after}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip() or "gh api compare failed")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("compare API returned a non-object payload")
    return payload


def changed_files(
    before: str,
    after: str,
    forced: str,
    compare: Callable[[str, str], dict[str, Any]],
    *,
    sleep: Callable[[float], Any] = time.sleep,
) -> list[str]:
    """Files in ``before...after``; raises :class:`FailOpen` when untrusted."""
    if not before or before == _ZERO_SHA:
        raise FailOpen("no previous commit on the branch (first push)")
    if not after:
        raise FailOpen("no head commit")
    if str(forced).lower() == "true":
        raise FailOpen("force push")
    if before == after:
        raise FailOpen("empty push (before == after)")
    payload: dict[str, Any] | None = None
    for attempt in (1, 2):
        try:
            payload = compare(before, after)
            break
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            if attempt == 2:
                raise FailOpen(f"compare API failed twice: {exc}") from exc
            sleep(5)
    assert payload is not None
    status = payload.get("status")
    if status != "ahead":
        raise FailOpen(f"compare status is {status!r}, not a fast-forward of the previous main")
    raw = payload.get("files")
    if not isinstance(raw, list):
        raise FailOpen("compare API listed no files")
    files: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        for key in ("filename", "previous_filename"):
            name = entry.get(key)
            if isinstance(name, str) and name.strip():
                files.append(name.strip())
    if len(raw) >= COMPARE_FILE_CAP:
        raise FailOpen(f"compare lists {len(raw)} files; the API caps at {COMPARE_FILE_CAP}, so the range may be truncated")
    if not files:
        raise FailOpen("compare API listed no files")
    return sorted(set(files))


def main() -> int:
    env = os.environ
    repo = env.get("REPO") or env.get("GITHUB_REPOSITORY") or ""
    if not repo:
        print("::warning::push_changed_files: no repository in the environment - failing open", file=sys.stderr)
        return 0
    try:
        files = changed_files(
            env.get("PUSH_BEFORE", ""),
            env.get("PUSH_AFTER", "") or env.get("GITHUB_SHA", ""),
            env.get("PUSH_FORCED", "false"),
            lambda before, after: compare_via_gh(repo, before, after),
        )
    except FailOpen as reason:
        print(f"::warning::push_changed_files: {reason} - failing open; all lanes run", file=sys.stderr)
        return 0
    print("\n".join(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
