"""Desktop approval must settle a real user response, including from a live loop.

The guardian's event-loop failure is separate from settlement. After escalate,
silence is not consent, an explicit answer must land through the gateway queue,
and a response scheduled on the same loop that is waiting must not be frozen
out until timeout.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from tools import approval_context


SESSION = "desktop-approval-loop"


@pytest.fixture()
def approval_env(monkeypatch):
    from tools import approval as mod

    saved = {
        "HERMES_SESSION_KEY": os.environ.get("HERMES_SESSION_KEY"),
        "HERMES_EXEC_ASK": os.environ.get("HERMES_EXEC_ASK"),
        "HERMES_INTERACTIVE": os.environ.get("HERMES_INTERACTIVE"),
        "HERMES_CRON_SESSION": os.environ.get("HERMES_CRON_SESSION"),
        "HERMES_SESSION_PLATFORM": os.environ.get("HERMES_SESSION_PLATFORM"),
    }
    monkeypatch.setenv("HERMES_SESSION_KEY", SESSION)
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "desktop")
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    yield mod
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _manual(monkeypatch, seconds):
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": "manual", "timeout": seconds})
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")


def _wait_for_queue(mod, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mod._gateway_queues.get(SESSION):
            return True
        time.sleep(0.005)
    return False


def test_approve_once_from_another_thread_settles_without_session_scope(approval_env, monkeypatch):
    mod = approval_env
    _manual(monkeypatch, 5)
    mod.register_gateway_notify(SESSION, lambda data: None)
    holder = {}

    def run():
        holder["result"] = mod.check_all_command_guards("rm -rf /var/cache/probe", "local")

    thread = threading.Thread(target=run)
    thread.start()
    assert _wait_for_queue(mod)
    resolved = mod.resolve_gateway_approval(SESSION, "once", request_id=mod._gateway_queues[SESSION][0].data["request_id"])
    thread.join(timeout=5)
    assert resolved == 1
    assert holder["result"]["approved"] is True
    assert holder["result"].get("user_approved") is True
    # once persists nothing; a later identical command still has to ask.
    assert SESSION not in mod._session_approved or not mod._session_approved[SESSION]


def test_explicit_deny_is_not_consent(approval_env, monkeypatch):
    mod = approval_env
    _manual(monkeypatch, 5)
    mod.register_gateway_notify(SESSION, lambda data: None)
    holder = {}
    thread = threading.Thread(target=lambda: holder.update(result=mod.check_all_command_guards("rm -rf /var/cache/probe", "local")))
    thread.start()
    assert _wait_for_queue(mod)
    mod.resolve_gateway_approval(SESSION, "deny", reason="no")
    thread.join(timeout=5)
    result = holder["result"]
    assert result["approved"] is False
    assert result.get("outcome") == "denied"
    assert result.get("user_consent") is False


def test_silence_is_timeout_not_consent(approval_env, monkeypatch):
    mod = approval_env
    _manual(monkeypatch, 0.05)
    mod.register_gateway_notify(SESSION, lambda data: None)
    result = mod.check_all_command_guards("rm -rf /var/cache/probe", "local")
    assert result["approved"] is False
    assert result.get("outcome") == "timeout"
    assert "Silence is not consent" in result["message"]


def test_stop_withdraws_without_counting_as_user_deny(approval_env, monkeypatch):
    from tools.interrupt import set_interrupt

    mod = approval_env
    _manual(monkeypatch, 5)
    mod.register_gateway_notify(SESSION, lambda data: None)
    holder = {}

    def run():
        set_interrupt(True, reason="turn interrupted")
        holder["result"] = mod.check_all_command_guards("rm -rf /var/cache/probe", "local")

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=5)
    result = holder["result"]
    assert result["approved"] is False
    assert result.get("outcome") == "cancelled"
    assert result.get("user_consent") is False


def test_late_and_wrong_session_responses_do_not_approve(approval_env, monkeypatch):
    mod = approval_env
    _manual(monkeypatch, 0.05)
    notified = []
    mod.register_gateway_notify(SESSION, lambda data: notified.append(data))
    result = mod.check_all_command_guards("rm -rf /var/cache/probe", "local")
    assert result.get("outcome") == "timeout"
    request_id = notified[0]["request_id"]
    assert mod.resolve_gateway_approval(SESSION, "once", request_id=request_id) == 0
    assert mod.resolve_gateway_approval("other-session", "once", request_id=request_id) == 0
    assert not mod._session_approved.get(SESSION)


def test_response_scheduled_on_the_waiting_loop_settles_before_timeout(approval_env, monkeypatch):
    """A client answer queued on the loop that is blocked in the approval wait must land."""
    mod = approval_env
    _manual(monkeypatch, 2)

    async def on_loop():
        loop = asyncio.get_running_loop()
        ticks = []

        def notify(data):
            def answer():
                ticks.append("answer")
                mod.resolve_gateway_approval(SESSION, "once", request_id=data["request_id"])
            loop.call_soon(answer)

        mod.register_gateway_notify(SESSION, notify)
        loop.call_soon(lambda: ticks.append("heartbeat"))
        result = mod.check_all_command_guards("rm -rf /var/cache/probe", "local")
        return result, ticks

    result, ticks = asyncio.run(on_loop())
    assert "heartbeat" in ticks
    assert result["approved"] is True
    assert result.get("user_approved") is True


def test_single_query_still_refuses_without_a_prompt(approval_env, monkeypatch):
    mod = approval_env
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setattr(mod, "_is_single_query_approval_context", lambda: True)
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": "manual", "timeout": 30})
    asked = []
    mod.register_gateway_notify(SESSION, lambda data: asked.append(data))
    result = mod.check_all_command_guards("rm -rf /var/cache/probe", "local")
    assert asked == []
    assert result["approved"] is False
    assert "single-query" in result["message"]


def test_undelivered_approval_frame_settles_instead_of_waiting_out_silence(monkeypatch):
    """A closed Desktop transport must not look like a user who never answered."""
    from tui_gateway import server_requests

    seen = []
    monkeypatch.setattr(server_requests, "_write", lambda frame: False)
    monkeypatch.setattr(server_requests, "_answerable", lambda sid: True)
    server_requests.send_async(
        "approval", "sid-1", {"request_id": "req-1", "command": "echo hi"}, seen.append,
    )
    assert seen == [None]
    assert all(req.sid != "sid-1" for req in server_requests._open.values())
