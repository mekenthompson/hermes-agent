#!/usr/bin/env python3
"""Bind a pushed OCI manifest to the locally scanned Docker image config."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-id", required=True, help="local Docker image config digest")
    parser.add_argument("--manifest", type=Path, required=True, help="raw remote OCI manifest JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not DIGEST.fullmatch(args.image_id):
        raise ValueError(f"invalid local image ID: {args.image_id!r}")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("remote manifest must be an object")
    config = manifest.get("config")
    if not isinstance(config, dict) or not isinstance(config.get("digest"), str):
        raise ValueError("remote manifest has no config digest")
    remote_id = config["digest"]
    if not DIGEST.fullmatch(remote_id):
        raise ValueError(f"invalid remote config digest: {remote_id!r}")
    if remote_id != args.image_id:
        raise ValueError(f"remote config digest {remote_id} does not match scanned image ID {args.image_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
