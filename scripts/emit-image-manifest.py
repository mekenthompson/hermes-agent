#!/usr/bin/env python3
"""Emit a deterministic handoff manifest for a published Agent image."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPOSITORY_RE = re.compile(r"^ghcr\.io/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def validated(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise SystemExit(f"invalid {label}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    repository = validated(args.repository, REPOSITORY_RE, "repository")
    revision = validated(args.revision, REVISION_RE, "revision")
    digest = validated(args.digest, DIGEST_RE, "digest")
    manifest = {
        "schema_version": 1,
        "repository": repository,
        "revision": revision,
        "digest": digest,
        "immutable_ref": f"{repository}@{digest}",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
