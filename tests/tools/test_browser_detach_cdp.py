"""Real target-level acceptance when the optional native browser is installed."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.platforms('linux')
@pytest.mark.live_system_guard_bypass
def test_real_native_targets_survive_client_exit_and_takeover():
    script = Path(__file__).with_name('browser_detach_runtime.py')
    result = subprocess.run([sys.executable, str(script)], env=dict(os.environ),
                            capture_output=True, text=True, timeout=240, check=False)
    if result.returncode == 77:
        pytest.skip('Optional Chromium/browser-harness unavailable; runtime image gate requires this test')
    assert result.returncode == 0, result.stderr + result.stdout
    assert '"ok": true' in result.stdout
