"""Explicit runner scratch selection keeps cleanup inside its owned subtree."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_tests_parallel as runner


class ScratchRootTests(unittest.TestCase):
    def test_override_is_scoped_and_cleanup_preserves_siblings(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(runner._runner_scratch_root(base))
            self.assertEqual(root.parent, Path(base))
            self.assertTrue(root.name.startswith("hermes-pytest"))
            sibling = Path(base) / "unrelated"
            sibling.mkdir()
            stale = root / "r-stale"
            stale.mkdir()
            old = 1
            os.utime(sibling, (old, old))
            os.utime(stale, (old, old))
            runner._sweep_killed_run_roots(str(root))
            self.assertTrue(sibling.is_dir())
            self.assertFalse(stale.exists())

    def test_default_uses_existing_disk_root_policy(self):
        # Observe path selection without creating anything in system temp.
        with patch.object(runner.os, "makedirs") as mkdir:
            root = runner._runner_scratch_root()
        name = "hermes-pytest" + (f"-{os.getuid()}" if hasattr(os, "getuid") else "")
        base = "/var/tmp" if os.name != "nt" and os.path.isdir("/var/tmp") else tempfile.gettempdir()
        self.assertEqual(root, os.path.join(base, name))
        mkdir.assert_called_once_with(root, exist_ok=True)

    def test_cli_passes_override_to_real_child_and_cleans_attempt(self):
        with tempfile.TemporaryDirectory() as base:
            base = Path(base)
            probe = base / "test_probe.py"
            receipt = base / "receipt"
            probe.write_text(
                "import os\nfrom pathlib import Path\n"
                "def test_location():\n"
                f"    Path({str(receipt)!r}).write_text(os.environ['TMPDIR'])\n"
                "    assert os.environ['TMPDIR'] == os.environ['PYTEST_DEBUG_TEMPROOT']\n"
            )
            result = subprocess.run(
                [sys.executable, str(Path(runner.__file__)), "--scratch-root", str(base),
                 "--files", str(probe), "-j", "1", "--file-retries", "0", "-q"],
                capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            attempt = Path(receipt.read_text())
            self.assertEqual(attempt.parent.parent, base)
            self.assertTrue(attempt.name.startswith("r-"))
            self.assertFalse(attempt.exists())
            self.assertTrue(probe.exists())


if __name__ == "__main__":
    unittest.main()
