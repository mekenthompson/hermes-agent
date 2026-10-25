"""A missing remote launch receipt retains one uncertain session for reconciliation."""

import json

import pytest

from tools import process_registry as registry_module
from tools.approval_context import approval_task_context, current_approval_task_context
from tools.terminal_tool_background import spawn_background_process


class _SandboxEnv:
    """Environment-backed (non-local) backend whose launch wrapper cannot run."""

    cwd = "/root"

    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    def get_temp_dir(self):
        return "/tmp"

    def execute(self, command, **kwargs):
        assert current_approval_task_context() is not None
        self.calls.append(command)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


@pytest.mark.parametrize("outcome", [
    {"output": 'exec: "bash": executable file not found in $PATH', "returncode": 127},
    RuntimeError("container 808ea027557c is not running"),
])
def test_missing_sandbox_receipt_retains_uncertain_session(outcome, monkeypatch, tmp_path):
    registry = registry_module.ProcessRegistry()
    monkeypatch.setattr(registry_module, "process_registry", registry)
    env = _SandboxEnv(outcome)

    with approval_task_context("Run this sandbox command once", identity="local:user", profile=str(tmp_path),
                               session="bg-fail-route", task_id="bg-fail", is_current=lambda: True):
        result = json.loads(spawn_background_process(
            command="sleep 30", env=env, env_type="docker",
            effective_task_id="bg-fail", task_id="bg-fail", session_key="bg-fail-route", workdir=None, cwd="/root",
            effective_pty=False, notify_on_complete=True, watch_patterns=None, approval_note=None,
            pty_disabled_reason=None,
        ))

    assert result["status"] == "unknown" and result["code"] == "process_launch_uncertain"
    assert result["exit_code"] is None and result["pid"] is None
    assert result["output"] != "Background process started"
    session = registry.get(result["session_id"])
    assert session.reconciliation_required == result["reconciliation_required"]
    assert session.pid_scope == "sandbox" and not session.exited
    assert registry._running == {session.id: session} and not registry._finished
    saved = json.loads(registry_module._checkpoint_path().read_text())
    assert [(entry["session_id"], entry["pid"], entry["pid_scope"]) for entry in saved] == [
        (session.id, None, "sandbox")]
    assert saved[0]["reconciliation_required"] == session.reconciliation_required
    assert len(env.calls) == 1
    assert not registry.pending_watchers
    assert "hint" not in result, "uncertain launch must not advise starting another process"
