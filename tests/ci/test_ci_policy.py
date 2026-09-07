"""Behavior contracts for CI orchestration policy."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "ci_policy.py"
_spec = importlib.util.spec_from_file_location("ci_policy", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load ci_policy.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["ci_policy"] = _mod
_spec.loader.exec_module(_mod)


@pytest.mark.parametrize(
    ("event_name", "repository", "ref", "expected"),
    [
        ("pull_request", "mekenthompson/hermes-agent", "refs/pull/1/merge", True),
        ("pull_request", "NousResearch/hermes-agent", "refs/pull/1/merge", True),
        ("push", "mekenthompson/hermes-agent", "refs/heads/main", True),
        ("push", "mekenthompson/hermes-agent", "refs/heads/feature", False),
        ("push", "NousResearch/hermes-agent", "refs/heads/main", False),
        ("workflow_dispatch", "mekenthompson/hermes-agent", "refs/heads/main", False),
    ],
)
def test_cancel_in_progress_preserves_upstream_pushes(
    event_name: str, repository: str, ref: str, expected: bool
) -> None:
    assert _mod.should_cancel_in_progress(event_name, repository, ref) is expected


@pytest.mark.parametrize(
    ("needs", "expected_failed"),
    [
        ({"tests": {"result": "success"}, "lint": {"result": "skipped"}}, []),
        ({"tests": {"result": "failure"}}, ["tests"]),
        ({"tests": {"result": "cancelled"}}, ["tests"]),
        (
            {
                "tests": {"result": "success"},
                "lint": {"result": "skipped"},
                "docs": {"result": "failure"},
                "scan": {"result": "cancelled"},
            },
            ["docs", "scan"],
        ),
    ],
)
def test_evaluate_needs_rejects_failure_and_cancelled_results(
    needs: dict[str, dict[str, str]], expected_failed: list[str]
) -> None:
    summary = _mod.evaluate_needs(needs)
    assert summary.compact == {name: info["result"] for name, info in needs.items()}
    assert summary.failed == expected_failed
