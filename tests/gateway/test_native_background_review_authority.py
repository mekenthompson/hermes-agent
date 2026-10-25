"""Native review producers retain their existing lease through thread and idle queues."""

import asyncio
import json
import threading

import pytest

from tests.gateway.test_native_terminal_background_authority import _runtime
from tests.agent.test_background_review import _bare_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("stopped", [False, True])
async def test_review_reservation_survives_parent_return_but_not_stop(tmp_path, monkeypatch, queued, stopped):
    from gateway.session_command_approval import command_task_scope
    from model_tools import handle_function_call
    from agent import background_review
    from agent.review_idle_queue import ReviewIdleQueue
    from tools.approval_context import current_approval_task_context

    _runner, authority, actor, ref, _registry = await _runtime(tmp_path, monkeypatch)
    agent = _bare_agent()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    effect, observed = tmp_path / "review.txt", []

    def review_model(*_args, **_kwargs):
        entered.set()
        try:
            assert release.wait(15)
            observed.append((current_approval_task_context(), json.loads(handle_function_call(
                "write_file", {"path": str(effect), "content": "reviewed"}, task_id="review-native"))))
        finally:
            done.set()

    monkeypatch.setattr(background_review, "_run_review_in_thread", review_model)
    queue = ReviewIdleQueue()
    monkeypatch.setattr(queue, "_ensure_thread", lambda: None)
    route = authority.sessions[ref.session_id].route
    try:
        with command_task_scope(authority, ref, actor.subject, "/refine", authority.route_epoch(route)):
            if queued:
                queue.enqueue(agent, route, dict(messages_snapshot=[], explicit=True))
            else:
                agent._spawn_background_review_now(messages_snapshot=[], explicit=True)
        if queued:
            item = queue._pending.pop(route)
            item.context.run(queue._dispatch, item)
        assert await asyncio.to_thread(entered.wait, 10)
        run = agent._background_review_run
        if stopped:
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
        release.set()
        assert await asyncio.to_thread(done.wait, 10)
        assert await asyncio.to_thread(run.request_done.wait, 10)
        evidence, result = observed[0]
        if stopped:
            assert evidence is None and result.get("code") == "task_authority_revoked", observed
            assert not effect.exists()
        else:
            assert evidence.identity == actor.subject and evidence.text == "/refine", observed
            assert effect.read_text() == "reviewed", result
    finally:
        release.set()


@pytest.mark.asyncio
async def test_stopped_idle_review_is_dropped_before_provider_setup(tmp_path, monkeypatch):
    from gateway.session_command_approval import command_task_scope
    from agent.review_idle_queue import ReviewIdleQueue

    _runner, authority, actor, ref, _registry = await _runtime(tmp_path, monkeypatch)
    agent = _bare_agent()
    queue = ReviewIdleQueue()
    monkeypatch.setattr(queue, "_ensure_thread", lambda: None)
    route = authority.sessions[ref.session_id].route
    with command_task_scope(authority, ref, actor.subject, "review when idle", authority.route_epoch(route)):
        queue.enqueue(agent, route, dict(messages_snapshot=[]))
    await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
    item = queue._pending.pop(route)
    item.context.run(queue._dispatch, item)
    assert agent._background_review_run is None
