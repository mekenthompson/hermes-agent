from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class E2EResultChartTests(unittest.TestCase):
    def test_empty_tags_do_not_fail_the_result_chart(self) -> None:
        proc = subprocess.run(
            [
                "node",
                "scripts/sandbox/generate-e2e-matrix.mjs",
                "--format",
                "results",
                "--tags",
                "",
            ],
            input='{"name":"leg","conclusion":"success"}\n',
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("leg", proc.stdout)


if __name__ == "__main__":
    unittest.main()
