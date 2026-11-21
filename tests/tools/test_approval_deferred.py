"""A gateway approval that times out is DEFERRED, not a dead end (HF-792).

The unanswered prompt parks answerable instead of resolving as BLOCKED: the agent is told the request
is still pending, a late answer is announced to the session, and the agent re-issuing the exact command
spends that answer through the normal gate. These tests drive the real ``check_all_command_guards`` /
``resolve_gateway_approval`` round trip (no mocked wait) and pin the security shape: the answer itself
never runs anything, a tightened floor still blocks, and scope is the exact command.
"""

from __future__ import annotations

import contextvars
import threading

import pytest

from tools import approval as mod
from tools import approval_context, approval_deferred, terminal_tool
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification

from tests.waiting import wait_until

KEY = "deferred-approval-session"
COMMAND = "rm -rf .git"


def _drain_events() -> list[dict]:
    events = []
    while not process_registry.completion_queue.empty():
        events.append(process_registry.completion_queue.get_nowait())
    return events


@pytest.fixture
def gateway(monkeypatch):
    """A gateway turn whose approval wait times out at once (``int(0.05)`` seconds)."""
    config = {"mode": "manual", "timeout": 0.05, "deferred_ttl": 3600}
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: config)
    for name in ("HERMES_YOLO_MODE", "HERMES_INTERACTIVE", "HERMES_CRON_SESSION", "HERMES_EXEC_ASK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", KEY)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-7")
    monkeypatch.setenv("HERMES_SESSION_ACTOR_ID", "alice")
    for state in (mod._gateway_queues, mod._gateway_notify_cbs, mod._session_approved, mod._permanent_approved,
                  approval_deferred._parked, approval_deferred._decisions):
        state.clear()
    _drain_events()
    cards: list[dict] = []
    mod.register_gateway_notify(KEY, cards.append)
    gateway.cards, gateway.config = cards, config
    yield gateway
    mod.unregister_gateway_notify(KEY)
    for state in (mod._gateway_queues, mod._session_approved, approval_deferred._parked, approval_deferred._decisions):
        state.clear()
    _drain_events()


def _defer(command: str = COMMAND) -> dict:
    result = mod.check_all_command_guards(command, "local")
    assert result["outcome"] == "deferred", result
    return result


def _answer(choice: str, reason: str | None = None, rid: str | None = None) -> int:
    """The gateway thread's answer: another thread/context than the one that asked."""
    rid = rid or gateway.cards[-1]["request_id"]
    return contextvars.Context().run(mod.resolve_gateway_approval, KEY, choice, reason=reason, request_id=rid)


# --- timeout: deferred, not blocked, not executed ---------------------------------------------------------------


def test_timeout_defers_and_is_neither_consent_nor_a_hard_block(gateway):
    result = _defer()

    assert result["approved"] is False and result["user_consent"] is False
    assert result["approval_deferred"] is True and result["request_id"] == gateway.cards[0]["request_id"]
    message = result["message"]
    assert message.startswith("DEFERRED") and "BLOCKED" not in message
    # The agent is told what to do: keep working, do not work around it, wait for the notice.
    assert "unrelated work" in message and "Do NOT rephrase" in message and "notification" in message
    assert len(gateway.cards) == 1


def test_terminal_tool_reports_a_deferred_status_and_runs_nothing(gateway):
    with pytest.raises(terminal_tool._Rejected) as rejected:
        terminal_tool._run_approval_guards(COMMAND, "local", {}, force=False)
    payload = __import__("json").loads(rejected.value.result_json)
    assert payload["status"] == "approval_deferred"
    assert payload["error"].startswith("DEFERRED")


def test_deferral_off_restores_the_hard_timeout(gateway):
    gateway.config["deferred_ttl"] = 0
    result = mod.check_all_command_guards(COMMAND, "local")
    assert result["outcome"] == "timeout" and result["message"].startswith("BLOCKED")
    assert mod.list_gateway_approvals(KEY) == []


# --- the card stays answerable -------------------------------------------------------------------------------------


def test_parked_card_is_answerable_by_id_only_and_is_not_blocking(gateway):
    settled: list[str] = []
    gateway.cards.clear()
    mod.register_gateway_notify(KEY, lambda data: (gateway.cards.append(data),
                                                   mod.register_gateway_settle(KEY, data["request_id"], settled.append)))
    _defer()
    rid = gateway.cards[0]["request_id"]

    assert settled == []  # the surface keeps the card: its hooks fire when the request really ends
    assert [d["request_id"] for d in mod.list_gateway_approvals(KEY)] == [rid]
    assert mod.ack_gateway_approval(KEY, rid) is True
    assert mod.has_deferred_approval(KEY) is True
    # No thread waits on it: not a busy backend, and a bare "yes" (a blocking approval) cannot answer it.
    assert mod.has_blocking_approval(KEY) is False
    assert mod.pending_gateway_approval_count() == 0
    assert mod.resolve_gateway_approval(KEY, "once") == 0
    assert mod.resolve_gateway_approval(KEY, "once", resolve_all=True) == 0
    assert mod.get_pending_gateway_approval(KEY) is None
    assert _answer("deny", rid=rid) == 1
    assert settled == ["resolved"]


def test_parked_card_obeys_the_same_authority_and_deny_only_rules_as_a_live_one(gateway):
    gateway.config["approvers"] = []  # only the requester (alice) and admins answer
    _defer()
    rid = gateway.cards[0]["request_id"]

    # A card is born Deny-only until its renderer proves it shows the whole command.
    assert mod.resolve_gateway_approval_card(KEY, "once", rid, actor_id="alice") == 0
    mod.restrict_gateway_approval_card_to_deny(KEY, rid, False)
    assert mod.resolve_gateway_approval_card(KEY, "once", rid, actor_id="mallory") == 0
    assert [d["request_id"] for d in mod.list_gateway_approvals(KEY)] == [rid]
    assert mod.resolve_gateway_approval_card(KEY, "once", rid, actor_id="alice") == 1
    assert mod.has_deferred_approval(KEY) is False


def test_unrecognized_late_answer_neither_consents_nor_discards_the_request(gateway):
    _defer()
    rid = gateway.cards[0]["request_id"]
    assert _answer("yes please", rid=rid) == 0
    assert mod.has_deferred_approval(KEY) is True and _drain_events() == []
    assert mod.check_all_command_guards(COMMAND, "local")["outcome"] == "deferred"


# --- late approve: announced, then spent only by re-issuing the exact command ----------------------------------


def test_late_approve_notifies_the_session_without_running_anything(gateway, monkeypatch):
    _defer()
    executed = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: executed.append(a))

    assert _answer("once") == 1

    (event,) = _drain_events()
    assert event["type"] == "approval_decision" and event["choice"] == "once"
    assert event["session_key"] == KEY and event["command"] == COMMAND
    assert (event["platform"], event["chat_id"]) == ("telegram", "chat-7")  # routed like a background notice
    text = format_process_notification(event)
    assert "APPROVED once" in text and "NOT been run" in text and "re-issue the exact same command" in text
    assert executed == [] and mod.is_approved(KEY, gateway.cards[0]["pattern_key"]) is False


def test_reissue_spends_a_late_once_approval_exactly_once(gateway):
    _defer()
    _answer("once")

    again = mod.check_all_command_guards(COMMAND, "local")

    assert again["approved"] is True and again.get("user_approved") is True
    assert len(gateway.cards) == 1  # the recorded answer stood in for a second card
    assert mod.is_approved(KEY, gateway.cards[0]["pattern_key"]) is False  # once persists nothing
    # Spent: the next identical command asks again (and defers again).
    assert mod.check_all_command_guards(COMMAND, "local")["outcome"] == "deferred"
    assert len(gateway.cards) == 2


def test_late_session_approval_persists_only_when_the_command_is_reissued(gateway):
    _defer()
    pattern_key = gateway.cards[0]["pattern_key"]
    _answer("session")
    assert mod.is_approved(KEY, pattern_key) is False

    assert mod.check_all_command_guards(COMMAND, "local")["approved"] is True

    assert mod.is_approved(KEY, pattern_key) is True
    assert mod.check_all_command_guards(COMMAND, "local")["approved"] is True
    assert len(gateway.cards) == 1


def test_late_always_approval_saves_through_the_normal_persistence_path(gateway, monkeypatch):
    saved = []
    monkeypatch.setattr(mod, "save_permanent_allowlist", saved.append)
    _defer()
    _answer("always")
    assert saved == []

    assert mod.check_all_command_guards(COMMAND, "local")["approved"] is True

    assert saved and gateway.cards[0]["pattern_key"] in saved[0]


def test_single_operation_cards_stay_single_operation_when_answered_late(gateway):
    """A card that offered no session/always scope (smart-DENY override, scan-incomplete, manual-only) is one
    operation however a client answers it, exactly as live."""
    _defer()
    entry = approval_deferred._parked[KEY][0]
    entry.data["allow_session"] = entry.data["allow_permanent"] = False
    _answer("always")

    assert mod.check_all_command_guards(COMMAND, "local")["approved"] is True
    assert mod.is_approved(KEY, gateway.cards[0]["pattern_key"]) is False
    assert mod.check_all_command_guards(COMMAND, "local")["outcome"] == "deferred"


def test_a_reworded_command_does_not_spend_the_answer(gateway):
    _defer()
    _answer("once")

    other = mod.check_all_command_guards("rm -rf .git/", "local")

    assert other["approved"] is False and other["outcome"] == "deferred"
    assert len(gateway.cards) == 2  # a new request: it prompted afresh
    assert mod.check_all_command_guards(COMMAND, "local")["approved"] is True  # the original answer is intact


def test_another_sessions_identical_command_cannot_spend_the_answer(gateway, monkeypatch):
    _defer()
    _answer("once")

    monkeypatch.setenv("HERMES_SESSION_KEY", "some-other-session")
    other = mod.check_all_command_guards(COMMAND, "local")

    assert other["approved"] is False


def test_a_different_requesters_identical_command_cannot_spend_the_answer(gateway, monkeypatch):
    gateway.config["approvers"] = []  # authority is then the requester's alone
    _defer()
    _answer("once")

    monkeypatch.setenv("HERMES_SESSION_ACTOR_ID", "mallory")

    assert mod.check_all_command_guards(COMMAND, "local")["approved"] is False


def test_a_floor_that_tightened_after_the_answer_still_blocks_the_reissue(gateway):
    _defer()
    _answer("once")
    gateway.config["deny"] = ["rm -rf .git*"]  # the operator added a deny rule meanwhile

    result = mod.check_all_command_guards(COMMAND, "local")

    assert result["approved"] is False and result.get("user_deny") is True
    gateway.config["deny"] = []
    assert mod.check_all_command_guards(COMMAND, "local")["approved"] is True  # the answer was never spent


def test_a_hardline_command_never_reaches_a_card_to_be_deferred(gateway):
    result = mod.check_all_command_guards("rm -rf /", "local")
    assert result["approved"] is False and gateway.cards == [] and not mod.has_deferred_approval(KEY)


# --- late deny -------------------------------------------------------------------------------------------------------


def test_late_deny_notifies_and_blocks_the_reissue_once(gateway):
    _defer()
    assert _answer("deny", reason="not on prod") == 1

    (event,) = _drain_events()
    text = format_process_notification(event)
    assert "DENIED" in text and "not on prod" in text and "Do NOT re-issue" in text

    again = mod.check_all_command_guards(COMMAND, "local")

    assert again["approved"] is False and again["outcome"] == "denied" and again["user_consent"] is False
    assert again["message"].startswith("BLOCKED") and "not on prod" in again["message"]
    assert len(gateway.cards) == 1  # a recorded refusal needs no second card
    assert mod.is_approved(KEY, gateway.cards[0]["pattern_key"]) is False
    # An in-time deny blocks one attempt and a retry prompts again; so does this.
    assert mod.check_all_command_guards(COMMAND, "local")["outcome"] == "deferred"


# --- pending, expiry, teardown ---------------------------------------------------------------------------------------


def test_reissuing_a_still_pending_command_defers_without_a_second_card(gateway):
    first = _defer()
    second = _defer()
    assert second["request_id"] == first["request_id"] and len(gateway.cards) == 1


def test_parked_cards_expire_and_late_answers_stop_being_spendable(gateway, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(approval_deferred.time, "monotonic", lambda: clock["now"])
    settled: list[str] = []
    gateway.cards.clear()
    mod.register_gateway_notify(KEY, lambda data: (gateway.cards.append(data),
                                                   mod.register_gateway_settle(KEY, data["request_id"], settled.append)))
    _defer()
    rid = gateway.cards[0]["request_id"]
    _answer("once")  # recorded, unspent

    clock["now"] += 3601
    assert mod.resolve_gateway_approval(KEY, "once", request_id=rid) == 0
    assert mod.check_all_command_guards(COMMAND, "local")["outcome"] == "deferred"  # the recorded answer expired
    assert len(gateway.cards) == 2
    clock["now"] += 3601
    approval_deferred.expire_due()
    assert settled == ["resolved", "timeout"] and not mod.has_deferred_approval(KEY)


def test_clearing_the_session_drops_parked_prompts_and_unspent_answers(gateway):
    _defer()
    _answer("once")
    _defer("rm -rf build")
    rid = gateway.cards[-1]["request_id"]

    mod.clear_session(KEY)

    assert _answer("once", rid=rid) == 0
    assert not approval_deferred._parked and not approval_deferred._decisions
    assert mod.check_all_command_guards(COMMAND, "local")["approved"] is False


def test_per_session_parking_is_bounded(gateway):
    for index in range(approval_deferred._MAX_PARKED_PER_SESSION + 3):
        _defer(f"rm -rf build{index}")
    assert len(approval_deferred._parked[KEY]) == approval_deferred._MAX_PARKED_PER_SESSION


def test_a_coalesced_follower_defers_on_the_leaders_request(gateway, monkeypatch):
    """Two parallel identical calls share ONE card; when it times out both are told the same DEFERRED request."""
    follower_joined = threading.Event()
    real_poll = mod._await_gateway_decision.__globals__["_poll_event"]
    real_follow = mod._await_gateway_decision.__globals__["_await_coalesced_leader"]

    def poll(event, session_key, **kwargs):
        if threading.current_thread().name == "leader":  # hold it until the follower has joined it
            follower_joined.wait(10)
        return real_poll(event, session_key, **kwargs)

    def follow(*args, **kwargs):
        follower_joined.set()
        return real_follow(*args, **kwargs)

    monkeypatch.setitem(mod._await_gateway_decision.__globals__, "_poll_event", poll)
    monkeypatch.setitem(mod._await_gateway_decision.__globals__, "_await_coalesced_leader", follow)
    results: dict[str, dict] = {}
    leader = threading.Thread(target=lambda: results.update(leader=mod.check_all_command_guards(COMMAND, "local")),
                              name="leader")
    leader.start()
    wait_until(lambda: mod._gateway_queues.get(KEY), what="the leader's pending approval")
    follower = threading.Thread(target=lambda: results.update(follower=mod.check_all_command_guards(COMMAND, "local")))
    follower.start()
    leader.join(10)
    follower.join(10)

    assert results["leader"]["outcome"] == results["follower"]["outcome"] == "deferred"
    assert results["leader"]["request_id"] == results["follower"]["request_id"]
    assert len(gateway.cards) == 1
