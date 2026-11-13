"""The image gate script must pass against the checkout's dashboard OAuth provider."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_verify_dashboard_oauth_image_passes(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "HERMES_HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/verify-dashboard-oauth-image.py")],
                          env=env, capture_output=True, text=True, timeout=120, check=False)
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], result
    assert result["checks"]["valid_signature_and_claims"]
    assert result["checks"]["scopeless_token_rejected"]
    assert proc.returncode == 0
