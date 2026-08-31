#!/usr/bin/env python3
"""Create a bounded package-level SPDX document for GitHub attestation."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DEFAULT_MAX_BYTES = 16_777_216
SPDX_ID_PATTERN = re.compile(r"SPDXRef-[A-Za-z0-9.-]+\Z")
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


def validate_spdx_id(value: object, label: str) -> str:
    if not isinstance(value, str) or SPDX_ID_PATTERN.fullmatch(value) is None:
        raise SystemExit(f"invalid SPDXID for {label}: {value!r}")
    return value


def find_removed_reference(value: object, removed_ids: set[str], path: str = "$") -> str | None:
    if isinstance(value, str):
        return path if value in removed_ids else None
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = find_removed_reference(item, removed_ids, f"{path}[{index}]")
            if found:
                return found
    if isinstance(value, dict):
        for key, item in value.items():
            found = find_removed_reference(item, removed_ids, f"{path}.{key}")
            if found:
                return found
    return None


def main() -> int:
    args = parse_args()
    if args.max_bytes <= 0:
        raise SystemExit("--max-bytes must be positive")

    document = json.loads(args.input.read_text(encoding="utf-8"))
    packages = document.get("packages")
    files = document.get("files")
    relationships = document.get("relationships")
    snippets = document.get("snippets", [])
    document_id = document.get("SPDXID")
    if not isinstance(packages, list) or not packages:
        raise SystemExit("SPDX document must contain at least one package")
    if not isinstance(files, list) or not isinstance(relationships, list):
        raise SystemExit("SPDX files and relationships must be arrays")
    if not isinstance(snippets, list):
        raise SystemExit("SPDX snippets must be an array")
    document_id = validate_spdx_id(document_id, "document")
    if not all(isinstance(relationship, dict) for relationship in relationships):
        raise SystemExit("SPDX relationships must be objects")

    compact_packages = []
    package_ids: set[str] = set()
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            raise SystemExit("SPDX packages must be objects")
        package_id = validate_spdx_id(package.get("SPDXID"), f"package {index}")
        if package_id in package_ids:
            raise SystemExit("package SPDXIDs must be present and unique")
        package_ids.add(package_id)
        compact_package = dict(package)
        for field in FILE_DERIVED_PACKAGE_FIELDS:
            compact_package.pop(field, None)
        compact_package["filesAnalyzed"] = False
        compact_packages.append(compact_package)

    file_ids: set[str] = set()
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise SystemExit("SPDX files must be objects")
        file_id = validate_spdx_id(entry.get("SPDXID"), f"file {index}")
        if file_id in file_ids:
            raise SystemExit("file SPDXIDs must be present and unique")
        file_ids.add(file_id)

    snippet_ids: set[str] = set()
    for index, entry in enumerate(snippets):
        if not isinstance(entry, dict):
            raise SystemExit("SPDX snippets must be objects")
        snippet_id = validate_spdx_id(entry.get("SPDXID"), f"snippet {index}")
        if snippet_id in snippet_ids:
            raise SystemExit("snippet SPDXIDs must be present and unique")
        snippet_ids.add(snippet_id)

    all_element_ids = package_ids | file_ids | snippet_ids | {document_id}
    if len(all_element_ids) != len(package_ids) + len(file_ids) + len(snippet_ids) + 1:
        raise SystemExit("SPDX element IDs must be globally unique")
    removed_ids = file_ids | snippet_ids

    compact_relationships = [
        relationship
        for relationship in relationships
        if relationship.get("spdxElementId") not in removed_ids
        and relationship.get("relatedSpdxElement") not in removed_ids
    ]
    known_ids = {document_id, *package_ids, *SPECIAL_REFERENCES}
    for relationship in compact_relationships:
        for field in ("spdxElementId", "relatedSpdxElement"):
            reference = relationship.get(field)
            if reference not in known_ids:
                raise SystemExit(f"dangling SPDX relationship reference: {field}={reference!r}")

    describes = document.get("documentDescribes")
    compact_describes = None
    if describes is not None:
        if not isinstance(describes, list) or not all(isinstance(item, str) for item in describes):
            raise SystemExit("SPDX documentDescribes must be an array of SPDXIDs")
        unknown_descriptions = set(describes) - package_ids - removed_ids
        if unknown_descriptions:
            raise SystemExit(f"unknown SPDX documentDescribes reference: {sorted(unknown_descriptions)!r}")
        compact_describes = [item for item in describes if item in package_ids]

    compact = dict(document)
    compact["packages"] = compact_packages
    compact["files"] = []
    compact["snippets"] = []
    compact["relationships"] = compact_relationships
    if compact_describes is not None:
        compact["documentDescribes"] = compact_describes
    removed_reference = find_removed_reference(compact, removed_ids)
    if removed_reference:
        raise SystemExit(f"removed SPDX element reference remains at {removed_reference}")
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
