#!/usr/bin/env python3
"""Extract the sole OCI manifest digest from Docker push output on stdin."""

from __future__ import annotations

import re
import sys

PUSH_SUMMARY = re.compile(
    r"(?m)^.+: digest: (sha256:[0-9a-f]{64}) size: [0-9]+\r?$"
)


def main() -> int:
    matches = PUSH_SUMMARY.findall(sys.stdin.read())
    if len(matches) != 1:
        print(
            f"error: expected exactly one Docker push digest summary, found {len(matches)}",
            file=sys.stderr,
        )
        return 1
    print(matches[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
