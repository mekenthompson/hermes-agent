from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
WORKFLOW = WORKFLOWS / "release-handoff.yml"
SCRIPT = ROOT / "scripts/ci/open_fleet_pin_pr.py"
DOC = ROOT / "docs/fork-agent-image.md"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def job_text() -> str:
    return workflow_text().split("\n  pin-fleet:\n", 1)[1]


class ReleaseHandoffWorkflowTests(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        for path in (WORKFLOW, SCRIPT, DOC):
            self.assertTrue(path.is_file(), path)

    def test_triggers_only_on_completed_fork_agent_image_runs_for_main(self) -> None:
        text = workflow_text()
        self.assertRegex(text, r"(?m)^on:\n  workflow_run:\n    workflows: \[\"Fork Agent Image\"\]\n    types: \[completed\]\n    branches: \[main\]\n")
        for forbidden in ("push:", "pull_request:", "pull_request_target", "workflow_dispatch:", "schedule:"):
            self.assertNotIn(forbidden, text)

    def test_job_guards_on_success_main_and_fork_repository(self) -> None:
        job = job_text()
        guard = job.split("if: >-\n", 1)[1].split("runs-on:", 1)[0]
        self.assertIn("github.event.workflow_run.conclusion == 'success'", guard)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", guard)
        # A fork PR can name its branch "main"; only push/dispatch runs from
        # this repository's own code may hand off with the App token.
        self.assertIn("github.event.workflow_run.event != 'pull_request'", guard)
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", guard)
        self.assertIn("github.repository == 'mekenthompson/hermes-agent'", guard)

    def test_handoffs_are_serialized_across_source_shas(self) -> None:
        text = workflow_text()
        self.assertIn("group: release-handoff-fleet", text)
        self.assertNotIn("group: release-handoff-${{ github.event.workflow_run.head_sha }}", text)

    def test_workflow_allows_the_twelve_minute_merge_wait(self) -> None:
        self.assertIn("timeout-minutes: 15", job_text())

    def test_artifact_is_downloaded_from_the_triggering_run_and_bound_to_its_head_sha(self) -> None:
        job = job_text()
        download = job.split("uses: actions/download-artifact@", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("run-id: ${{ github.event.workflow_run.id }}", download)
        self.assertIn("name: agent-image-manifest-${{ github.event.workflow_run.head_sha }}", download)
        self.assertIn("github-token: ${{ github.token }}", download)
        # The script binds the manifest revision to the triggering run's SHA, never main HEAD.
        self.assertIn('--expected-revision "$SOURCE_SHA"', job)
        self.assertIn("SOURCE_SHA: ${{ github.event.workflow_run.head_sha }}", workflow_text())

    def test_workflow_never_uses_github_sha_or_ref(self) -> None:
        # Under workflow_run these are main HEAD, not the published commit; only comments may mention them.
        code = "\n".join(line for line in workflow_text().splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"github\.sha\b")
        self.assertNotRegex(code, r"github\.ref\b")

    def test_missing_artifact_ends_the_job_successfully_with_a_notice(self) -> None:
        job = job_text()
        locate = job.split("name: Locate the handoff manifest artifact", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("actions/runs/$SOURCE_RUN_ID/artifacts", locate)
        self.assertIn("::notice::", locate)
        self.assertIn('echo "found=false"', locate)
        self.assertNotIn("exit 1", locate)
        for step in ("Download the handoff manifest", "Mint a hermes-fleet-scoped App token", "Open the auto-merging pin PR"):
            body = job.split(f"name: {step}", 1)[1].split("\n      - name:", 1)[0]
            self.assertIn("if: steps.locate.outputs.found == 'true'", body, step)
        self.assertNotIn("continue-on-error", job)

    def test_app_token_is_scoped_to_hermes_fleet_only(self) -> None:
        job = job_text()
        token = job.split("uses: actions/create-github-app-token@", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("app-id: ${{ vars.RELEASE_BOT_APP_ID }}", token)
        self.assertIn("private-key: ${{ secrets.RELEASE_BOT_PRIVATE_KEY }}", token)
        self.assertIn("owner: mekenthompson", token)
        self.assertRegex(token, r"(?m)^\s+repositories: hermes-fleet\s*$")
        self.assertNotIn("hermes-fleet-private", token)
        self.assertNotIn("hermes-agent", token)
        # The App token is used only by the pin step, never by checkout or download.
        self.assertEqual(job.count("steps.app-token.outputs.token"), 1)
        self.assertIn("GH_TOKEN: ${{ steps.app-token.outputs.token }}", job.split("name: Open the auto-merging pin PR", 1)[1])
        self.assertIn("persist-credentials: false", job)

    def test_workflow_holds_no_write_or_packages_permission(self) -> None:
        text = workflow_text()
        self.assertNotIn("packages:", text)
        self.assertNotIn("id-token:", text)
        self.assertNotIn("write", text.split("\njobs:", 1)[0].split("permissions:", 1)[1].split("\n\n", 1)[0])
        job = job_text()
        job_permissions = job.split("permissions:\n", 1)[1].split("steps:", 1)[0]
        self.assertEqual(job_permissions.split(), ["actions:", "read", "contents:", "read"])

    def test_app_private_key_is_referenced_only_by_this_workflow(self) -> None:
        holders = sorted(p.name for p in WORKFLOWS.glob("*.y*ml") if "RELEASE_BOT_PRIVATE_KEY" in p.read_text(encoding="utf-8"))
        self.assertEqual(holders, [WORKFLOW.name])
        holders = sorted(p.name for p in WORKFLOWS.glob("*.y*ml") if "RELEASE_BOT_APP_ID" in p.read_text(encoding="utf-8"))
        self.assertEqual(holders, [WORKFLOW.name])

    def test_image_publishing_workflow_still_carries_no_secrets(self) -> None:
        self.assertNotIn("secrets.", (WORKFLOWS / "fork-agent-image.yml").read_text(encoding="utf-8"))

    def test_every_external_action_is_sha_pinned(self) -> None:
        text = workflow_text()
        actions = re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", text)
        self.assertGreaterEqual(len(actions), 3)
        for action in actions:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", action)
        self.assertIn("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", text)
        self.assertIn("actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349", text)

    def test_pin_step_runs_the_script_with_auto_merge_and_source_run(self) -> None:
        job = job_text()
        pin = job.split("name: Open the auto-merging pin PR", 1)[1]
        self.assertIn("python3 scripts/ci/open_fleet_pin_pr.py", pin)
        self.assertIn('--source-run-url "$SOURCE_RUN_URL"', pin)
        self.assertIn("--fleet-repo mekenthompson/hermes-fleet", pin)
        self.assertNotIn("--dry-run", pin)
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"pr", "merge", "--auto", "--squash"', script)

    def test_documentation_describes_the_fleet_handoff(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        section = text.split("## Fleet handoff", 1)[1]
        for needle in (
            "release-handoff.yml",
            "workflow_run",
            "hermes-release-bot",
            "release/pin-agent-",
            "release/agent-image-manifest.json",
            "auto-merge",
            "head_sha",
            "--dry-run",
        ):
            self.assertIn(needle, section, needle)


if __name__ == "__main__":
    unittest.main()
