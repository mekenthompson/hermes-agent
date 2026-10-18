"""A committed viewer submit carries its own request into the smart reviewer."""
import asyncio
from contextvars import copy_context
from types import SimpleNamespace
import threading

import pytest

from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_viewer_request_reaches_reviewer_but_internal_notice_does_not(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_automation import admit_route_wake
    from gateway.session_contract import Principal, Submission
    from gateway.session_local import create_local_session
    from hermes_state import SessionDB
    from tools import approval as A, approval_context
    from tools.approval_context import current_approval_task_context
    from tools.approval_smart import _smart_approve
    from tools.delegate_tool_child_run import _ChildRun

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    seen = []

    def guardian(**kwargs):
        prompt = kwargs["messages"][1]["content"]
        seen.append(prompt)
        # Checks delivery and lifecycle, not the model's semantic judgment.
        answer = "APPROVE" if "Edit only src/cli.py" in prompt else "ESCALATE"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", guardian)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    db = SessionDB(tmp_path / "state.db")
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner", db=db)
    results = {}
    copied = []

    async def answer(*, message, session_key, run_generation, **_kwargs):
        live = authority.sessions[ref.session_id]
        assert session_key == live.route and run_generation > 0
        def review():
            evidence = current_approval_task_context()
            if evidence is not None:
                copied.append(copy_context())
            verdict = _smart_approve("python -c 'print(1)'", "script execution")
            gate = (A.check_all_command_guards("python -c 'print(1)'", "local")
                    if evidence is not None else None)
            def child_conversation(**_kwargs):
                return {"evidence": current_approval_task_context(),
                        "verdict": _smart_approve("python -c 'print(1)'", "script execution")}

            child_run = _ChildRun(
                child=SimpleNamespace(session_id="child-session", run_conversation=child_conversation),
                parent_agent=SimpleNamespace(), task_index=0, goal="Edit src/cli.py",
                subagent_id=None, child_progress_cb=None, child_task_id="child-1")
            child_result, child_error, _deferred = child_run.await_child()
            assert child_error is None
            child = child_result["evidence"]
            assert child_result["verdict"] == verdict
            return evidence, child, verdict, gate

        results[message] = await asyncio.to_thread(review)
        return {"final_response": "ok", "messages": []}

    monkeypatch.setattr(runner, "_run_agent", answer)
    try:
        viewer = Principal("test-owner", "default", frozenset(
            {"session:create", "session:submit", "session:read", "session:approve"}), "viewer")
        ref = create_local_session(authority, viewer, dict(request_id="cli", source="cli", model="fixture", toolsets=[]))
        live = authority.sessions[ref.session_id]
        await authority.submit(viewer, Submission("typed", ref, {"text": "Edit only src/cli.py"}, "queue"))
        await async_wait_until(lambda: "Edit only src/cli.py" in results, timeout=10, what="viewer request")
        adapter = runner._adapter_for_source(live.source)
        assert await admit_route_wake(runner, adapter, live.source, "synthetic notice", "tick") is True
        await async_wait_until(lambda: "synthetic notice" in results, timeout=10, what="internal notice")
        from hermes_state_runtime import get_session_admission
        await async_wait_until(
            lambda: get_session_admission(db, admission_id=results["Edit only src/cli.py"][0].task_id)["status"] == "terminal",
            timeout=10, what="settled viewer admission")
        assert copied[0].run(current_approval_task_context) is None
    finally:
        db.close()

    evidence, child, verdict, gate = results["Edit only src/cli.py"]
    assert evidence.text == "Edit only src/cli.py"
    assert evidence.identity == "test-owner"
    assert child.ancestry == ("child-1",)
    assert verdict == "approve"
    assert gate["approved"] is True and gate["smart_approved"] is True
    assert results["synthetic notice"] == (None, None, "escalate", None)
    assert "Edit only src/cli.py" in seen[0]
    assert "Edit only src/cli.py" not in seen[-1]


@pytest.mark.asyncio
async def test_stop_during_smart_review_rejects_late_approve(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal, Submission
    from gateway.session_local import create_local_session
    from hermes_state import SessionDB
    from tools import approval as A, approval_context

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    started, release = threading.Event(), threading.Event()
    results = []

    def guardian(**_kwargs):
        started.set()
        assert release.wait(10)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="APPROVE"))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", guardian)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    db = SessionDB(tmp_path / "state.db")
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner", db=db)

    async def answer(event):
        live = authority.sessions[ref.session_id]
        event._approval_run_generation = runner._begin_session_run_generation(live.route)
        results.append(await asyncio.to_thread(A.check_all_command_guards, "python -c 'print(1)'", "local"))
        return "ok"

    monkeypatch.setattr(runner, "_handle_message", answer)
    viewer = Principal("test-owner", "default", frozenset(
        {"session:create", "session:submit", "session:read", "session:control"}), "viewer")
    ref = create_local_session(authority, viewer, dict(request_id="cli", source="cli", model="fixture", toolsets=[]))
    try:
        await authority.submit(viewer, Submission("typed", ref, {"text": "Edit src/cli.py"}, "queue"))
        assert await asyncio.to_thread(started.wait, 10)
        generation = authority.sessions[ref.session_id].event_stream.execution["execution_generation"]
        await authority.interrupt(viewer, ref, generation)
        release.set()
        await async_wait_until(lambda: bool(results), timeout=10, what="late smart verdict")
    finally:
        release.set()
        db.close()
    assert results[0]["approved"] is False
    assert results[0]["outcome"] == "stale_task"


def test_foreign_and_synthetic_rows_cannot_supply_human_request():
    from contextlib import nullcontext
    from gateway.config import Platform
    from gateway.session_ingress import _approval_task_scope
    from hermes_state_runtime import RuntimeStoreError

    source = SimpleNamespace(user_id="owner", platform=Platform.LOCAL, profile=None,
                             chat_id="room", thread_id=None)
    live = SimpleNamespace(source=source, route="route")
    ref = SimpleNamespace(session_id="session")
    authority = SimpleNamespace(profile_id="profile", sessions={"session": live})
    row = {"admission_id": "a", "generation": 2, "request_id": "r", "principal_id": "intruder",
           "payload": {"text": "Edit src/cli.py"}}
    event = SimpleNamespace(internal=False, source=source, text=row["payload"]["text"])
    with pytest.raises(RuntimeStoreError, match="permission_denied"):
        _approval_task_scope(authority, ref, row, live, event, is_api=False)
    event.internal = True
    assert isinstance(_approval_task_scope(authority, ref, row, live, event, is_api=False), type(nullcontext()))
    event.internal = False
    row["payload"] = {"text": event.text, "native_text_v1": {}}
    assert isinstance(_approval_task_scope(authority, ref, row, live, event, is_api=False), type(nullcontext()))
