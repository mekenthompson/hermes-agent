"""Behavior contracts for CI orchestration policy."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
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
    ("needs", "selected", "expected_failed"),
    [
        ({}, {}, ["selected_jobs"]),
        ({"tests": {"result": "success"}}, {"tests": True, "lint": True}, ["lint"]),
        ({"tests": {"result": "skipped"}}, {"tests": True}, ["tests"]),
        ({"tests": {"result": "skipped"}}, {"tests": False}, []),
        ({"tests": {"result": "cancelled"}}, {"tests": False}, ["tests"]),
        ({"tests": {"result": "failure"}}, {"tests": True}, ["tests"]),
        ({"tests": {}}, {"tests": True}, ["tests"]),
        ({"tests": {"result": "skipped"}}, {"tests": "false"}, ["selected_jobs"]),
    ],
)
def test_evaluate_needs_requires_selected_success_and_explicit_non_applicability(
    needs: dict[str, dict[str, str]],
    selected: dict[str, bool],
    expected_failed: list[str],
) -> None:
    assert _mod.evaluate_needs(needs, selected).failed == expected_failed


def test_selection_uses_boolean_values_and_fork_scope() -> None:
    lanes = {
        key: "false"
        for key in (
            "python",
            "python_prod",
            "frontend",
            "os_tests",
            "installer",
            "bootstrap",
            "uv_lock",
            "ci_review",
            "mcp_catalog",
            "scan",
            "deps",
            "npm_lock",
            "docker_meta",
            "rust",
            "site",
            "binary_artifacts",
        )
    }
    selected = _mod.selected_jobs(lanes, "mekenthompson/hermes-agent", "pull_request")
    assert selected["tests"] is False
    assert selected["tests-os"] is False
    assert selected["installer-tests"] is False
    assert selected["history-check"] is True
    lanes["python"] = "true"
    lanes["os_tests"] = "true"
    assert (
        _mod.selected_jobs(lanes, "mekenthompson/hermes-agent", "pull_request")[
            "tests-os"
        ]
        is True
    )


def test_selection_covers_each_orchestrator_prerequisite() -> None:
    yaml = pytest.importorskip("hermes_yaml")
    workflow = yaml.safe_load(
        (_PATH.parents[2] / ".github/workflows/ci.yaml").read_text()
    )
    jobs = workflow["jobs"]
    expected = set(jobs) - {"all-checks-pass", "ci-timings"}
    selected = _mod.selected_jobs({}, "mekenthompson/hermes-agent", "pull_request")
    assert set(selected) == expected == set(jobs["all-checks-pass"]["needs"])


def test_upgrade_applicability_reaches_real_upgrade_suite() -> None:
    yaml = pytest.importorskip("hermes_yaml")
    ci = yaml.safe_load((_PATH.parents[2] / ".github/workflows/ci.yaml").read_text())
    tests = yaml.safe_load(
        (_PATH.parents[2] / ".github/workflows/tests.yml").read_text()
    )
    assert (
        ci["jobs"]["tests"]["with"]["upgrade"]
        == "${{ needs.detect.outputs.upgrade == 'true' }}"
    )
    assert tests[True]["workflow_call"]["inputs"]["upgrade"]["default"] is True
    assert tests["jobs"]["e2e-upgrade-plan"]["if"] == "inputs.upgrade"
    assert tests["jobs"]["e2e-upgrade"]["if"] == "inputs.upgrade"


def test_review_label_gate_uses_critical_finding_result() -> None:
    needs = {"review-labels": {"result": "skipped"}}
    selected = {"review-labels": False}
    assert _mod.evaluate_needs(needs, selected).failed == []
    assert _mod.evaluate_needs(needs, selected, critical_findings=True).failed == [
        "review-labels"
    ]


def test_policy_file_runs_as_the_aggregate_job_entrypoint(tmp_path: Path) -> None:
    """The checkout-provided script writes the output consumed by the workflow job."""
    output_path = tmp_path / "github-output"
    result = subprocess.run(
        [sys.executable, str(_PATH)],
        input=json.dumps({"tests": {"result": "success"}}),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(output_path),
            "SELECTED_JOBS": '{"tests":true}',
        },
        check=True,
    )

    assert "All selected checks passed" in result.stdout
    assert output_path.read_text(encoding="utf-8") == 'needs-json={"tests":"success"}\n'


def test_workflow_selection_output_is_boolean_json(tmp_path: Path) -> None:
    output_path = tmp_path / "selected"
    result = subprocess.run(
        [sys.executable, str(_PATH), "--select"],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(output_path),
            "LANES": '{"python":"false","python_prod":"true","frontend":"false","upgrade":"false"}',
            "REPO": "mekenthompson/hermes-agent",
            "EVENT_NAME": "pull_request",
        },
    )
    line = output_path.read_text().strip()
    assert result.stdout.strip() == line
    selected = json.loads(line.removeprefix("selected_jobs="))
    assert selected["tests"] is False
    assert selected["e2e-desktop-core"] is True
    assert selected["e2e-desktop-update"] is False
