"""Fork review gates for CI-sensitive changes and MCP catalog changes."""

from __future__ import annotations

from pathlib import PurePosixPath

_CI_REVIEW_FILES = {".prettierrc"}
_CI_REVIEW_PATHS = (".github/workflows/", ".github/actions/")
_MCP_CATALOG_PATHS = ("optional-mcps/",)
_MCP_CATALOG_FILES = {"hermes_cli/mcp_catalog.py"}


def is_ci_review(path: str) -> bool:
    if path in _CI_REVIEW_FILES or path.startswith(_CI_REVIEW_PATHS):
        return True
    return PurePosixPath(path).name.startswith("eslint.config.")


def is_mcp_catalog(path: str) -> bool:
    return path.startswith(_MCP_CATALOG_PATHS) or path in _MCP_CATALOG_FILES


def ci_review_files(files: list[str]) -> list[str]:
    """Return CI-sensitive paths that require explicit maintainer review."""
    return sorted({path.strip() for path in files if path.strip() and is_ci_review(path.strip())})


def review_lanes(files: list[str]) -> dict[str, bool]:
    """Classify fork review gates without coupling them to upstream lane rules."""
    return {
        "mcp_catalog": any(is_mcp_catalog(path) for path in files),
        "ci_review": any(is_ci_review(path) for path in files),
    }
