"""Desktop slash RPC obtains task authority only from its existing authenticated owner."""
import threading

import pytest

from tests.tui_gateway.test_approval_request_origin import LoginSocket
from tests.tools.test_delegate_apiserver_background import _fake_parent, _patch_delegate
from tui_gateway import server


@pytest.mark.parametrize("identity", ["owner", "unscoped", "foreign"])
def test_desktop_review_producer_and_stop(tmp_path, monkeypatch, identity):
    from hermes_state import SessionDB
    from tools import async_delegation as delegation
    from tui_gateway.transport import bind_transport, reset_transport

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dt = _patch_delegate(monkeypatch)
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    for name in ("_schedule_agent_build", "_schedule_session_cap_enforcement", "_register_session_cwd",
                 "_start_session_services"):
        monkeypatch.setattr(server, name, lambda *args: None)
    release, started = threading.Event(), threading.Event()

    def child(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return {"task_index": 0, "status": "completed", "summary": "reviewed"}

    monkeypatch.setattr(dt, "_run_single_child", child)
    delegation._reset_for_tests()
    transport = LoginSocket()
    token = bind_transport(transport)
    sid = None
    try:
        reply = server.handle_request({"id": "create", "method": "session.create",
            "params": {"source": "desktop", "cwd": str(tmp_path)}})
        sid = reply["result"]["session_id"]
        session = server._sessions[sid]
        parent = _fake_parent()
        parent.session_id = session["session_key"]
        session["agent"] = parent
        session["history"] = [{"role": "user", "content": "Review the implementation"}]
        if identity == "unscoped":
            transport.auth_identity = {"provider": "dashboard-session-token", "user_id": "dashboard-session-token"}
        elif identity == "foreign":
            transport.auth_identity = {"provider": "basic", "user_id": "another-user"}
        result = server.handle_request({"id": "review", "method": "slash.exec",
            "params": {"session_id": sid, "command": "/review Check the implementation"}})
        if identity == "foreign":
            assert "native task-scoped authority" in result["result"]["output"], result
            assert not started.is_set()
        elif identity == "unscoped":
            assert "native task-scoped authority" in result["result"]["output"]
            assert not started.is_set()
        else:
            assert "Review started" in result["result"]["output"], result
            assert started.wait(10)
            leases = list(session["_approval_task_leases"])
            assert len(leases) == 1 and leases[0].live()
            stopped = server.handle_request({"id": "stop", "method": "session.interrupt",
                                             "params": {"session_id": sid}})
            assert stopped["result"]["status"] == "interrupted"
            assert not leases[0].live()
    finally:
        release.set()
        reset_transport(token)
        delegation._reset_for_tests()
        if sid is not None:
            server._sessions.pop(sid, None)
        db.close()
