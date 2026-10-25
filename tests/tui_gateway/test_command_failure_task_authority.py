"""Authenticated legacy Desktop commands revoke child authority on failure or cancellation."""

import asyncio
import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_authenticated_review_child_obeys_command_outcome(tmp_path, monkeypatch, outcome):
    from agent import review_engine
    from hermes_state import SessionDB
    from model_tools import handle_function_call
    from tests.tui_gateway.test_approval_request_origin import LoginSocket
    from tools.approval_context import background_approval_task, reserve_background_approval_task
    from tui_gateway import server
    from tui_gateway.transport import bind_transport, reset_transport

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    for name in ("_schedule_agent_build", "_schedule_session_cap_enforcement", "_register_session_cwd", "_start_session_services"):
        monkeypatch.setattr(server, name, lambda *_args: None)
    reservations = []

    def review(*_args):
        reservations.append(reserve_background_approval_task())
        assert reservations[-1] is not None
        if outcome == "failure":
            raise RuntimeError("review failed after dispatch")
        if outcome == "cancelled":
            raise asyncio.CancelledError("review cancelled after dispatch")
        return {"status": "dispatched"}

    monkeypatch.setattr(review_engine, "start_review", review)
    token = bind_transport(LoginSocket())
    sid = None
    try:
        created = server.handle_request({"id": "create", "method": "session.create",
                                         "params": {"source": "desktop", "cwd": str(tmp_path)}})
        sid = created["result"]["session_id"]
        session = server._sessions[sid]
        session["agent"] = SimpleNamespace(session_id=session["session_key"])
        session["history"] = [{"role": "user", "content": "Review the implementation"}]
        request = {"id": "review", "method": "slash.exec",
                   "params": {"session_id": sid, "command": "/review Check this change"}}
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                server.handle_request(request)
        else:
            result = server.handle_request(request)
            assert "Review started" in result["result"]["output"] if outcome == "success" else "review failed after dispatch" in result["result"]["output"]
        reservation = reservations[0]
        assert reservation.evidence.identity == "basic:alice"
        assert reservation.evidence.task_id.startswith("command:")
        effect = tmp_path / "review-child.txt"
        with background_approval_task(reservation, "review-child"):
            reply = json.loads(handle_function_call("write_file", {"path": str(effect), "content": "reviewed"},
                                                   task_id="review-child"))
        if outcome == "success":
            assert effect.read_text() == "reviewed", reply
        else:
            assert reply.get("code") == "task_authority_revoked", reply
            assert not effect.exists()
    finally:
        for reservation in reservations:
            reservation.release()
        reset_transport(token)
        if sid is not None:
            server._sessions.pop(sid, None)
        db.close()
