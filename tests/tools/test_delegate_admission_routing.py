"""Delegation launch routes run before a child can become a writer."""
from __future__ import annotations

from types import SimpleNamespace

from hermes_cli import admission_contract
from hermes_cli.admission_contract import AdmissionErrorCode, AdmissionResult
from tools import delegate_tool


def _rejected(request_id: str) -> AdmissionResult:
    return AdmissionResult(
        request_id=request_id,
        state="rejected",
        reason=AdmissionErrorCode.UNSUPPORTED_PATH.value,
    )


def test_public_delegate_route_rejects_before_child_construction(monkeypatch):
    parent = SimpleNamespace(_delegate_depth=0, session_id="parent-session")
    built: list[object] = []
    credentials = {
        "model": "test-model", "provider": None, "base_url": None,
        "api_key": None, "api_mode": None, "command": None, "args": None,
    }

    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 1)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "_get_max_concurrent_children", lambda: 1)
    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", lambda *_args: credentials)
    monkeypatch.setattr(
        delegate_tool,
        "_build_children",
        lambda *_args, **_kwargs: (built.append(object()), ([], None))[1],
    )
    monkeypatch.setattr(
        admission_contract,
        "route_launch_path",
        lambda _path, _caller, *, request_id: _rejected(request_id),
    )

    result = delegate_tool.delegate_task(goal="do not construct", parent_agent=parent)

    assert "admission path rejected: unsupported_path" in result
    assert built == []


def test_direct_child_helper_rejects_before_child_execution(monkeypatch):
    started: list[object] = []
    monkeypatch.setattr(
        admission_contract,
        "route_launch_path",
        lambda _path, _caller, *, request_id: _rejected(request_id),
    )
    monkeypatch.setattr(
        delegate_tool,
        "_fabricated_entry",
        lambda *_args: started.append(object()) or {"status": "error"},
    )
    monkeypatch.setattr(
        delegate_tool,
        "_start_heartbeat",
        lambda *_args: (_ for _ in ()).throw(AssertionError("heartbeat must follow admission")),
    )
    monkeypatch.setattr(
        delegate_tool,
        "_register_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("registration must follow admission")),
    )

    result = delegate_tool._run_single_child(0, "do not run", child=SimpleNamespace())

    assert result == {"status": "error"}
    assert len(started) == 1


def test_writer_admission_boundary_preserves_direct_read_only_seams():
    """Only production-stamped or concrete source-writing children are writers."""
    from run_agent import AIAgent

    direct_writer = object.__new__(AIAgent)
    setattr(direct_writer, "valid_tool_names", {"write_file"})
    direct_reader = object.__new__(AIAgent)
    setattr(direct_reader, "valid_tool_names", {"read_file"})

    assert delegate_tool._requires_writer_admission(
        SimpleNamespace(_delegate_admission_required=True)
    )
    assert delegate_tool._requires_writer_admission(direct_writer)
    assert not delegate_tool._requires_writer_admission(direct_reader)
    assert not delegate_tool._requires_writer_admission(
        SimpleNamespace(_subagent_id="subagent-test", valid_tool_names={"terminal"})
    )
