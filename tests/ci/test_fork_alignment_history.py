"""The alignment health gate needs both frozen ancestors, not depth two."""
from pathlib import Path

import yaml

from tests.ci import workflow_steps

ROOT = Path(__file__).resolve().parents[2]


def test_alignment_health_checkout_keeps_history_and_identity_guards():
    workflow = yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())
    job = workflow["jobs"]["lint"]
    checkout = next(step for step in job["steps"] if step.get("name") == "Checkout code")
    # Consolidated lint needs full history for both the ratchet and its diff.
    assert str(checkout["with"]["fetch-depth"]) == "0"
    ratchet = next(step for step in job["steps"] if step.get("name") == "Ratchet the PR against the base branch tip")
    ctx = {
        "inputs": {"python": False, "event_name": "pull_request"},
        "steps": {"tools": {"outcome": "success"}},
        "__status__": {"success": False, "cancelled": False},
    }
    # Frontend PRs still run health, even if an earlier combined check failed.
    workflow_steps.required(job, ctx)
    workflow_steps.required(ratchet, ctx)
    assert ratchet["env"]["CI_PR_HEAD_REPOSITORY"] == "${{ github.event.pull_request.head.repo.full_name }}"
    assert ratchet["run"] == "python scripts/check --only health --base HEAD^1"
