from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / ".git").exists())
SCRIPT = ROOT / "scripts" / "verify-exact-main-ci.py"
SHA = "a" * 40


def run(*, sha=SHA, branch="main", workflow=".github/workflows/ci.yml", status="completed", conclusion="success", created_at="2026-09-07T01:00:00Z"):
    return {"event": "push", "head_sha": sha, "head_branch": branch, "path": workflow, "status": status, "conclusion": conclusion, "created_at": created_at}


class ExactMainCiGateTests(unittest.TestCase):
    job_name = "publish"
    workflow_name = "fork-agent-image.yml"
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("exact_main_ci", SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.module)

    def test_gate_accepts_latest_success_for_exact_main_ci(self):
        self.assertTrue(self.module.latest_exact_main_ci_is_green([run()], SHA, ".github/workflows/ci.yml"))

    def test_gate_rejects_wrong_sha(self):
        self.assertFalse(self.module.latest_exact_main_ci_is_green([run(sha="b" * 40)], SHA, ".github/workflows/ci.yml"))

    def test_gate_rejects_wrong_branch(self):
        self.assertFalse(self.module.latest_exact_main_ci_is_green([run(branch="release")], SHA, ".github/workflows/ci.yml"))

    def test_gate_rejects_wrong_workflow(self):
        self.assertFalse(self.module.latest_exact_main_ci_is_green([run(workflow=".github/workflows/other.yml")], SHA, ".github/workflows/ci.yml"))

    def test_gate_rejects_latest_pending_after_older_success(self):
        runs = [run(created_at="2026-09-07T01:00:00Z"), run(status="in_progress", conclusion=None, created_at="2026-09-07T02:00:00Z")]
        self.assertFalse(self.module.latest_exact_main_ci_is_green(runs, SHA, ".github/workflows/ci.yml"))

    def test_gate_rejects_latest_failure_after_older_success(self):
        runs = [run(created_at="2026-09-07T01:00:00Z"), run(conclusion="failure", created_at="2026-09-07T02:00:00Z")]
        self.assertFalse(self.module.latest_exact_main_ci_is_green(runs, SHA, ".github/workflows/ci.yml"))

    def test_gate_rejects_empty_and_api_failure(self):
        self.assertFalse(self.module.latest_exact_main_ci_is_green([], SHA, ".github/workflows/ci.yml"))
        with self.assertRaises(RuntimeError):
            self.module.fetch_runs("owner/repo", "ci.yaml", SHA, lambda *_, **__: (_ for _ in ()).throw(RuntimeError("api failure")))

    def test_fetch_runs_filters_server_side_by_head_sha_without_filtering_status(self):
        requested = []
        pending = {"status": "in_progress", "conclusion": None}
        result = type("Result", (), {"returncode": 0, "stdout": '{"workflow_runs": [{"status": "in_progress", "conclusion": null}]}'})()
        self.assertEqual(self.module.fetch_runs("owner/repo", "ci.yaml", SHA, lambda command, **_: requested.append(command) or result), [pending])
        query = requested[0][-1]
        self.assertIn(f"head_sha={SHA}", query)
        self.assertNotIn("status=", query)

    def _wait(self, polls, *, timeout_seconds=1800, interval_seconds=20):
        """Drive wait_for_exact_main_ci with a fake clock; each poll advances it by one interval."""
        polls = list(polls)
        fetches, slept, now = [], [], [0.0]
        def fetch():
            fetches.append(now[0])
            return polls.pop(0) if len(polls) > 1 else polls[0]
        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
        result = self.module.wait_for_exact_main_ci(
            fetch, SHA, ".github/workflows/ci.yml",
            timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
            clock=lambda: now[0], sleep=sleep, log=lambda _message: None,
        )
        return result, fetches, slept

    def test_wait_passes_when_in_progress_run_completes_successfully(self):
        pending = run(status="in_progress", conclusion=None)
        result, fetches, slept = self._wait([[pending], [pending], [run()]])
        self.assertTrue(result)
        self.assertEqual(len(fetches), 3)
        self.assertEqual(slept, [20, 20])

    def test_wait_fails_when_in_progress_run_completes_with_failure(self):
        pending = run(status="in_progress", conclusion=None)
        result, fetches, _ = self._wait([[pending], [run(conclusion="failure")]])
        self.assertFalse(result)
        self.assertEqual(len(fetches), 2)

    def test_wait_fails_closed_on_timeout_without_sleeping_past_the_deadline(self):
        pending = run(status="in_progress", conclusion=None)
        result, fetches, slept = self._wait([[pending]], timeout_seconds=60, interval_seconds=20)
        self.assertFalse(result)
        self.assertEqual(slept, [20, 20, 20])
        self.assertEqual(len(fetches), 4)

    def test_wait_keeps_polling_while_no_run_exists_yet_then_honours_it(self):
        result, fetches, _ = self._wait([[], [], [run()]])
        self.assertTrue(result)
        self.assertEqual(len(fetches), 3)
        result, _, _ = self._wait([[]], timeout_seconds=40)
        self.assertFalse(result)

    def test_wait_honours_a_newer_run_that_supersedes_an_older_one(self):
        older_success = run(created_at="2026-09-07T01:00:00Z")
        newer_pending = run(status="in_progress", conclusion=None, created_at="2026-09-07T02:00:00Z")
        newer_failed = run(conclusion="failure", created_at="2026-09-07T02:00:00Z")
        result, fetches, _ = self._wait([[older_success, newer_pending], [older_success, newer_failed]])
        self.assertFalse(result)
        self.assertEqual(len(fetches), 2)
        older_cancelled = run(conclusion="cancelled", created_at="2026-09-07T01:00:00Z")
        older_pending = run(status="in_progress", conclusion=None, created_at="2026-09-07T01:00:00Z")
        newer_success = run(created_at="2026-09-07T02:00:00Z")
        result, _, _ = self._wait([[older_pending], [older_cancelled, newer_success]])
        self.assertTrue(result)

    def test_wait_does_not_sleep_when_the_first_poll_is_already_complete(self):
        result, fetches, slept = self._wait([[run()]])
        self.assertTrue(result)
        self.assertEqual(len(fetches), 1)
        self.assertEqual(slept, [])
        result, _, slept = self._wait([[run(conclusion="failure")]])
        self.assertFalse(result)
        self.assertEqual(slept, [])

    def test_cli_wait_flags_default_to_thirty_minutes_and_twenty_seconds(self):
        import subprocess
        result = subprocess.run(["python3", str(SCRIPT), "--help"], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--wait", "--timeout-minutes", "--interval-seconds"):
            self.assertIn(flag, result.stdout)

    def test_publish_gate_waits_for_ci(self):
        import yaml
        text = (ROOT / ".github/workflows/" / self.workflow_name).read_text()
        publish = yaml.safe_load(text)["jobs"][self.job_name]["steps"]
        gates = [s for s in publish if "scripts/verify-exact-main-ci.py" in s.get("run", "")]
        self.assertEqual(len(gates), 1)
        self.assertIn("--wait", gates[0]["run"])

    def test_gate_authenticates_github_api(self):
        import yaml
        text = (ROOT / ".github/workflows/" / self.workflow_name).read_text()
        jobs = yaml.safe_load(text)["jobs"]
        gates = [s for j in jobs.values() for s in j.get("steps", [])
                 if "scripts/verify-exact-main-ci.py" in s.get("run", "")]
        self.assertTrue(gates)
        for gate in gates:
            self.assertEqual(gate.get("env", {}).get("GH_TOKEN"), "${{ github.token }}")

    def test_publish_job_effectively_has_actions_read(self):
        lines = (ROOT / ".github/workflows/" / self.workflow_name).read_text().splitlines()
        publish = lines.index(f"  {self.job_name}:")
        block = "\n".join(lines[publish:])
        self.assertIn("    permissions:\n      contents: read\n      actions: read", block)
