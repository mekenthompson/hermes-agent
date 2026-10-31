"""The alignment health gate needs both frozen ancestors, not depth two."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_alignment_health_checkout_keeps_history_and_identity_guards():
    workflow = yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())
    job = workflow["jobs"]["code-health"]
    checkout = next(step for step in job["steps"] if step.get("name") == "Checkout code")
    # String '0' is truthy in Actions; numeric 0 falls through to depth two.
    assert checkout["with"]["fetch-depth"] == (
        "${{ github.repository == 'mekenthompson/hermes-agent' && "
        "github.event.pull_request.head.repo.full_name == github.repository && "
        "github.head_ref == 'house/hf576-nous-6bb377a51d' && '0' || '2' }}"
    )
    ratchet = next(step for step in job["steps"] if step.get("name") == "Ratchet the PR against the base branch tip")
    assert ratchet["env"]["CI_PR_HEAD_REPOSITORY"] == "${{ github.event.pull_request.head.repo.full_name }}"
    assert ratchet["run"] == "python scripts/check --only health --base HEAD^1"
