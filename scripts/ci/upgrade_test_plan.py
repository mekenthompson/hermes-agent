"""Discover upgrade suites once and keep every file in exactly one CI job."""

from __future__ import annotations

import json
import os
from pathlib import Path

FORK = "mekenthompson/hermes-agent"
LONG_FILES = {"core": "test_upgrade_path.py", "git": "test_shallow_install.py"}


def upgrade_matrix(root: Path, repository: str) -> dict:
    suites = {"core": sorted(root.glob("test_*.py"))}
    suites.update(
        (directory.name, sorted(directory.rglob("test_*.py")))
        for directory in sorted(root.iterdir())
        if directory.is_dir() and not directory.name.startswith(("_", "."))
    )
    include = []
    for suite, files in suites.items():
        if not files or (repository == FORK and suite == "handoff"):
            continue  # Existing fork exclusion: requires upstream's larger runner.
        buckets = [files]
        if repository == FORK and suite in LONG_FILES:
            long = [file for file in files if file.name == LONG_FILES[suite]]
            rest = [file for file in files if file not in long]
            # Each long file itself starts several real installs. Give it its
            # own four-core machine; split the other files across two machines.
            buckets = [bucket for bucket in (long, rest[::2], rest[1::2]) if bucket]
        elif repository == FORK and suite == "pm":
            # Every PM file stages real installs and generation updates. Six
            # simultaneous trees became the tail after core/git were split.
            # Cap files per machine, including newly added lifecycle scenarios.
            groups = (len(files) + 2) // 3
            buckets = [files[index::groups] for index in range(groups)]
        for index, bucket in enumerate(buckets, 1):
            include.append({
                "shard": suite if len(buckets) == 1 else f"{suite}-{index}/{len(buckets)}",
                "files": [file.relative_to(root).as_posix() for file in bucket],
            })
    if not include:
        raise ValueError("upgrade suite contains no test files")
    return {"include": include}


def main() -> None:
    matrix = upgrade_matrix(Path("tests/e2e/core/upgrade"), os.environ["GITHUB_REPOSITORY"])
    output = "shards=" + json.dumps(matrix, separators=(",", ":"))
    print(output)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(output + "\n")


if __name__ == "__main__":
    main()
