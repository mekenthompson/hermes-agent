from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    ".github/workflows/tests.yml": "ubuntu-latest-96-core",
    ".github/workflows/nix.yml": "ubuntu-latest-32-core",
    ".github/workflows/e2e-desktop.yml": "ubuntu-latest-32-core",
    ".github/workflows/rust-tests.yml": "ubuntu-latest-32-core",
    ".github/workflows/js-tests.yml": "ubuntu-latest-32-core",
}


class ForkRunnerFallbackTests(unittest.TestCase):
    def test_upstream_large_runners_have_standard_fork_fallbacks(self) -> None:
        trust_guard = (
            "github.repository == 'NousResearch/hermes-agent' && "
            "(github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository)"
        )
        for relative, large_runner in EXPECTED.items():
            with self.subTest(workflow=relative):
                lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
                expected = (
                    f"    runs-on: ${{{{ {trust_guard} && "
                    f"'{large_runner}' || 'ubuntu-latest' }}}}"
                )
                self.assertIn(expected, lines)

        tests_lines = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8").splitlines()
        expected_workers = (
            f"          HERMES_TEST_WORKERS: ${{{{ {trust_guard} && "
            "'96' || '4' }}"
        )
        self.assertIn(expected_workers, tests_lines)
        self.assertIn("    timeout-minutes: 60", tests_lines)

    def test_windows_large_runner_has_standard_fork_fallback(self) -> None:
        trust_guard = (
            "github.repository == 'NousResearch/hermes-agent' && "
            "(github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository)"
        )
        lines = (ROOT / ".github/workflows/tests-os.yml").read_text(encoding="utf-8").splitlines()
        expected = (
            "    runs-on: ${{ matrix.name == 'Windows-only tests' && "
            f"{trust_guard} && 'windows-latest-32-core' || matrix.runner }}}}"
        )
        self.assertIn(expected, lines)
        self.assertIn("            runner: windows-latest", lines)

    def test_docker_large_runners_reject_untrusted_fork_prs(self) -> None:
        trust_guard = (
            "github.repository == 'NousResearch/hermes-agent' && "
            "(github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository)"
        )
        lines = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8").splitlines()
        expected = f"    if: {trust_guard} && needs.detect.outputs.build == 'true'"
        self.assertIn(expected, lines)


if __name__ == "__main__":
    unittest.main()
