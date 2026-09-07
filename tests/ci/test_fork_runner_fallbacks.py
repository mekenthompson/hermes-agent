from __future__ import annotations

import json
import re
import runpy
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
    def test_matrix_covers_entire_suite_for_fork_and_upstream(self) -> None:
        text = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        line = next(x for x in text.splitlines() if "matrix:" in x)
        choices = [json.loads(x) for x in re.findall(r"'(\{.*?\})'", line)]
        self.assertEqual(choices, [{"slice": [1, 2, 3, 4, 5, 6], "slices": [6]}, {"slice": [1], "slices": [1]}])
        self.assertIn("github.repository == 'mekenthompson/hermes-agent'", line)
        runner = runpy.run_path(str(ROOT / "scripts/run_tests_parallel.py"))
        files = runner["_discover_files"]([ROOT / "tests"])
        self.assertGreater(len(files), 100)
        for choice in choices:
            buckets = runner["_compute_lpt_slices"](files, choice["slices"][0], {}, ROOT)
            selected = [p for i in choice["slice"] for p in buckets[i - 1]]
            self.assertEqual(sorted(selected), sorted(files))
            self.assertEqual(len(selected), len(set(selected)))

    def test_js_matrix_shards_ui_project_for_fork_only(self) -> None:
        text = (ROOT / ".github/workflows/js-tests.yml").read_text(encoding="utf-8")
        line = next(x for x in text.splitlines() if "matrix:" in x)
        self.assertIn("github.repository == 'mekenthompson/hermes-agent'", line)
        fork, upstream = [json.loads(x) for x in re.findall(r"'(\{.*?\})'", line)]
        self.assertEqual(upstream, {"unit": ["all"]})
        units = fork["unit"]
        self.assertEqual(units[0], "checks")
        shards = [u.removeprefix("ui-") for u in units[1:]]
        counts = {int(x.split("/")[1]) for x in shards}
        self.assertEqual(len(counts), 1)
        total = counts.pop()
        self.assertEqual([int(x.split("/")[0]) for x in shards], list(range(1, total + 1)))
        for unit in ("all)", "checks)", "ui-*)"):
            self.assertIn(f"            {unit}", text.splitlines())
        # The skipped unit must be the same command the ui shards run.
        self.assertIn("--skip 'apps/desktop :: check:test:ui'", text)
        self.assertIn("npm run --prefix apps/desktop test:ui -- --shard=", text)
        scripts = json.loads((ROOT / "apps/desktop/package.json").read_text(encoding="utf-8"))["scripts"]
        self.assertEqual(scripts["check:test:ui"], "npm run test:ui")
        self.assertEqual(scripts["test:ui"], "vitest run --project ui")

    def test_fork_skips_upstream_only_packaging_workflows(self) -> None:
        for relative in (".github/workflows/nix.yml", ".github/workflows/docker.yml"):
            with self.subTest(workflow=relative):
                lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
                start = lines.index("  detect:")
                block = lines[start : start + 12]
                self.assertIn("    if: github.repository == 'NousResearch/hermes-agent'", block)

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
        self.assertFalse(any("matrix." in line for line in tests_lines if line.startswith("    if:")))
        self.assertIn("          scripts/run_tests.sh --slice ${{ matrix.slice }}/${{ matrix.slices }}", tests_lines)
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
