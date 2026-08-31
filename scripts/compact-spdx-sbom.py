#!/usr/bin/env python3
"""Create a bounded package-level SPDX document for GitHub attestation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_MAX_BYTES = 16_777_216
SPECIAL_REFERENCES = {"NONE", "NOASSERTION"}
FILE_DERIVED_PACKAGE_FIELDS = {
    "hasFiles",
    "licenseInfoFromFiles",
    "packageVerificationCode",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_bytes <= 0:
        raise SystemExit("--max-bytes must be positive")

    document = json.loads(args.input.read_text(encoding="utf-8"))
    packages = document.get("packages")
    files = document.get("files")
    relationships = document.get("relationships")
    document_id = document.get("SPDXID")
    if not isinstance(packages, list) or not packages:
        raise SystemExit("SPDX document must contain at least one package")
    if not isinstance(files, list) or not isinstance(relationships, list):
        raise SystemExit("SPDX files and relationships must be arrays")
    if not isinstance(document_id, str) or not document_id:
        raise SystemExit("SPDX document must have an SPDXID")

    compact_packages = []
    for package in packages:
        if not isinstance(package, dict):
            raise SystemExit("SPDX packages must be objects")
        compact_package = dict(package)
        for field in FILE_DERIVED_PACKAGE_FIELDS:
            compact_package.pop(field, None)
        compact_package["filesAnalyzed"] = False
        compact_packages.append(compact_package)

    package_ids = {package.get("SPDXID") for package in compact_packages}
    file_ids = {entry.get("SPDXID") for entry in files}
    if None in package_ids or len(package_ids) != len(packages):
        raise SystemExit("package SPDXIDs must be present and unique")
    if None in file_ids or len(file_ids) != len(files):
        raise SystemExit("file SPDXIDs must be present and unique")

    compact_relationships = [
        relationship
        for relationship in relationships
        if relationship.get("spdxElementId") not in file_ids
        and relationship.get("relatedSpdxElement") not in file_ids
    ]
    known_ids = {document_id, *package_ids, *SPECIAL_REFERENCES}
    for relationship in compact_relationships:
        for field in ("spdxElementId", "relatedSpdxElement"):
            reference = relationship.get(field)
            if reference not in known_ids:
                raise SystemExit(f"dangling SPDX relationship reference: {field}={reference!r}")

    compact = dict(document)
    compact["packages"] = compact_packages
    compact["files"] = []
    compact["relationships"] = compact_relationships
    payload = (json.dumps(compact, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > args.max_bytes:
        raise SystemExit(f"attestation SBOM is {len(payload)} bytes; limit is {args.max_bytes}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(args.output)
    print(
        f"attestation SBOM: {len(packages)} packages, "
        f"{len(compact_relationships)} relationships, {len(payload)} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
