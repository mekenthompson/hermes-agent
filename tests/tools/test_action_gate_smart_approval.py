"""Smart approvals on the shared action gate.

Under ``approvals.mode: smart`` a computer_use action reaches the guardian LLM
before a human, exactly like a flagged terminal command: APPROVE runs it
without a prompt, ESCALATE still asks. Plugin rules that demand a human keep
the human-only path, and manual mode never consults the guardian.
"""

import json

import pytest

from tools import approval
from tools import approval_context
from tools import approval_prompt
from tools.approval import request_tool_approval
from tools.computer_use import tool as cua_tool


@pytest.fixture(autouse=True)
def _cli_owner_present(monkeypatch):
    monkeypatch.setattr(approval, "get_current_session_key", lambda default="default": "smart-gate-session")
    monkeypatch.setattr(approval_context, "get_current_session_key", lambda default="default": "smart-gate-session")
    monkeypatch.setattr(approval, "is_approved", lambda sk, pk: False)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False, raising=False)
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: None, raising=False)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval_context, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(cua_tool, "_approval_callback", None)


def _mode(monkeypatch, mode):
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: mode)


def _record_prompts(monkeypatch, answer):
    prompts = []

    def prompt(*args, **kwargs):
        prompts.append(args)
        return answer

    monkeypatch.setattr(approval, "prompt_dangerous_approval", prompt)
    monkeypatch.setattr(approval_prompt, "prompt_dangerous_approval", prompt)
    return prompts


def _record_guardian(monkeypatch, verdict):
    seen = []

    def guardian(command, description, pattern_key, pattern_keys, session_key):
        seen.append((command, pattern_key))
        return verdict

    monkeypatch.setattr(approval, "_smart_verdict", guardian)
    return seen


def test_smart_approve_runs_computer_use_action_without_prompt(monkeypatch):
    _mode(monkeypatch, "smart")
    seen = _record_guardian(monkeypatch, "approve")
    prompts = _record_prompts(monkeypatch, "deny")

    assert cua_tool._request_approval("click", {"action": "click", "element": 4}) is None
    assert seen == [("computer_use: click element #4", "cua:click:background")]
    assert prompts == []


def test_guardian_sees_the_value_a_set_value_would_enter(monkeypatch):
    _mode(monkeypatch, "smart")
    seen = _record_guardian(monkeypatch, "approve")
    _record_prompts(monkeypatch, "deny")

    assert cua_tool._request_approval("set_value", {"action": "set_value", "value": "hunter2", "element": 7}) is None
    assert seen == [("computer_use: set_value 'hunter2' on element #7", "cua:set_value:background")]


def test_smart_escalate_still_asks_the_owner(monkeypatch):
    _mode(monkeypatch, "smart")
    seen = _record_guardian(monkeypatch, "escalate")
    prompts = _record_prompts(monkeypatch, "deny")

    err = cua_tool._request_approval("type", {"action": "type", "text": "hello"})

    assert len(seen) == 1
    assert len(prompts) == 1
    assert err is not None
    assert "denied" in json.loads(err)["error"].lower()


def test_manual_mode_never_consults_guardian(monkeypatch):
    _mode(monkeypatch, "manual")
    seen = _record_guardian(monkeypatch, "approve")
    prompts = _record_prompts(monkeypatch, "once")

    assert cua_tool._request_approval("click", {"action": "click", "element": 1}) is None
    assert seen == []
    assert len(prompts) == 1


def test_plugin_rule_keeps_human_only_path_under_smart(monkeypatch):
    _mode(monkeypatch, "smart")
    seen = _record_guardian(monkeypatch, "approve")
    prompts = _record_prompts(monkeypatch, "once")

    assert request_tool_approval("write_file", "plugin demands a human")["approved"] is True
    assert seen == []
    assert len(prompts) == 1
