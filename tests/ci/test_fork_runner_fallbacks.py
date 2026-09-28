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
        self.assertEqual(choices, [{"slice": [1, 2, 3, 4, 5, 6, 7, 8], "slices": [8]}, {"slice": [1], "slices": [1]}])
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
                self.assertTrue(
                    any(
                        line.strip().startswith("if: github.repository == 'NousResearch/hermes-agent'")
                        for line in block
                    ),
                    block,
                )

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
        bare = []
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped in {
                    "runs-on: ubuntu-latest-32-core",
                    "runs-on: ubuntu-latest-96-core",
                    "runs-on: windows-latest-32-core",
                }:
                    bare.append(f"{path.relative_to(ROOT)}: {stripped}")
        self.assertEqual(bare, [])
        large_labels = (
            "ubuntu-latest-32-core",
            "ubuntu-latest-32-arm-core",
            "ubuntu-latest-96-core",
            "windows-latest-32-core",
            "windows-latest-32-arm-core",
        )
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip().startswith("runs-on:"):
                    continue
                if not any(label in line for label in large_labels) and "matrix.runner" not in line:
                    continue
                if path.name == "tests-os.yml":
                    continue  # its inverse predicate explicitly selects hosted Windows on forks
                self.assertIn(trust_guard, line, f"untrusted runner selection: {path.name}: {line.strip()}")

    def test_windows_large_runner_has_standard_fork_fallback(self) -> None:
        trust_guard = (
            "github.repository == 'NousResearch/hermes-agent' && "
            "(github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository)"
        )
        lines = (ROOT / ".github/workflows/tests-os.yml").read_text(encoding="utf-8").splitlines()
        fallback = next(line for line in lines if line.startswith("    runs-on: ${{ startsWith(matrix.runner"))
        self.assertIn("startsWith(matrix.runner, 'windows-')", fallback)
        self.assertIn("github.repository != 'NousResearch/hermes-agent'", fallback)
        self.assertIn("github.event_name == 'pull_request'", fallback)
        self.assertIn("github.event.pull_request.head.repo.full_name != github.repository", fallback)
        self.assertIn("'windows-latest' || matrix.runner", fallback)
        workers = next(line for line in lines if "HERMES_TEST_WORKERS:" in line)
        self.assertIn(trust_guard, workers)
        self.assertIn("runner.arch == 'ARM64' && '2'", workers)
        self.assertIn("'8') || '2'", workers)

    def test_nested_matrix_target_runner_falls_back_on_untrusted_forks(self) -> None:
        fallback = (
            "startsWith(matrix.target.runner, 'windows-latest-32') && "
            "(github.repository != 'NousResearch/hermes-agent' || "
            "(github.event_name == 'pull_request' && "
            "github.event.pull_request.head.repo.full_name != github.repository)) && "
            "'windows-latest' || matrix.target.runner"
        )
        hits = []
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith("runs-on:"):
                    continue
                if "matrix.target.runner" not in stripped:
                    continue
                hits.append(f"{path.name}: {stripped}")
                self.assertIn(fallback, stripped, f"unguarded nested runner: {path.name}: {stripped}")
        self.assertIn("pm-bundle.yml: runs-on: ${{ " + fallback + " }}", hits)

    def test_docker_large_runners_reject_untrusted_fork_prs(self) -> None:
        trust_guard = (
            "github.repository == 'NousResearch/hermes-agent' && "
            "(github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository)"
        )
        lines = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8").splitlines()
        build_runner = f"    runs-on: ${{{{ {trust_guard} && matrix.runner || 'ubuntu-latest' }}}}"
        publish_runner = f"    runs-on: ${{{{ {trust_guard} && 'ubuntu-latest-32-core' || 'ubuntu-latest' }}}}"
        self.assertIn(build_runner, lines)
        self.assertIn(publish_runner, lines)
        publish_if = next(line for line in lines if line.strip().startswith("if: needs.mode.outputs.phase == 'publish'"))
        self.assertIn("github.repository == 'NousResearch/hermes-agent'", publish_if)


if __name__ == "__main__":
    unittest.main()
