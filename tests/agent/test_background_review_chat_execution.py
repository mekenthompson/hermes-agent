"""The post-turn review runs under its own lifecycle, not the settled turn's gateway execution.

``_spawn_background_review_now`` copies the dispatcher's contextvars into the review thread. Before
the fix that carried the turn's chat-execution identity, whose row settles when the turn's worker
returns, so every review tool call was refused ("Durable tool observation refused") and no memory
or skill was ever saved.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent import background_review
import agent.tool_executor as executor
import model_tools
from agent.background_review import cancel_background_review_for_live_turn
from agent.chat_execution_context import bind_chat_execution, observe_tool_before_execution
from agent.tool_executor import _resolve_sequential_dispatch, _ToolCallRef
from gateway.chat_execution_recovery import ChatExecutionStore
from run_agent import AIAgent
from tests.agent.test_background_review import _bare_agent

_WAIT_S = 10


def _started_turn(home, key="key"):
    store = ChatExecutionStore(home)
    generation = store.reserve(key, 0, 0)
    assert generation
    assert store.bind_worker_started(key, generation, "session")
    return store, generation


def _tool_observed(home, key="key"):
    with ChatExecutionStore(home)._db() as db:
        return db.execute("SELECT tool_observed FROM executions WHERE session_key=?", (key,)).fetchone()[0]


def _review_tool_call(monkeypatch):
    """One real sequential tool dispatch, as the review fork's tool loop performs it."""
    monkeypatch.setattr(executor, "_start_quiet_tool_spinner", lambda *a, **k: None)
    monkeypatch.setattr(model_tools, "handle_function_call", lambda *a, **k: "saved")
    fork = SimpleNamespace(
        _context_engine_tool_names=set(), _memory_manager=None, quiet_mode=False, session_id="session",
        _current_turn_id="", _current_api_request_id="", valid_tool_names=[], enabled_toolsets=None,
        disabled_toolsets=None,
    )
    call = _ToolCallRef(name="probe_tool", args={}, task_id="task", call_id="call", trace=[])
    return _resolve_sequential_dispatch(fork, call, []).execute({})


class _Review:
    """Stands in for ``_run_review_in_thread``: waits for the gate, then runs the review's tool call."""

    def __init__(self, agent, monkeypatch, tool_call):
        self.gate, self.finished, self.outcome = threading.Event(), threading.Event(), []
        self._agent, self._tool_call = agent, tool_call
        monkeypatch.setattr(background_review, "_run_review_in_thread", self)

    def __call__(self, agent, messages_snapshot, prompt, task_cfg=None, review_run=None, **kwargs):
        try:
            assert self.gate.wait(_WAIT_S)
            if review_run is not None and review_run.cancel_requested.is_set():
                self.outcome.append("cancelled")
                return
            try:
                self.outcome.append(self._tool_call())
            except Exception as exc:
                self.outcome.append(f"refused: {exc}")
        finally:
            background_review.finish_background_review_run(agent, review_run)
            self.finished.set()


def _spawn(agent):
    AIAgent._spawn_background_review_now(
        agent, messages_snapshot=[{"role": "user", "content": "hello"}], review_memory=True, review_skills=True)


def test_review_tool_call_runs_after_the_parent_turn_settled(tmp_path, monkeypatch):
    store, generation = _started_turn(tmp_path)
    review = _Review(_bare_agent(), monkeypatch, lambda: _review_tool_call(monkeypatch))
    agent = review._agent
    with bind_chat_execution(tmp_path, "key", generation):
        _spawn(agent)
    # The gateway's run_turn_worker settles the turn; only then does the review reach its tool.
    assert store.worker_finished("key", generation)
    review.gate.set()
    assert review.finished.wait(_WAIT_S)

    assert review.outcome == ["saved"]
    assert _tool_observed(tmp_path) == 0  # the review never flags the settled turn as having run a tool


def test_releasing_the_review_leaves_the_parent_turn_fenced(tmp_path, monkeypatch):
    store, generation = _started_turn(tmp_path)
    review = _Review(_bare_agent(), monkeypatch, lambda: observe_tool_before_execution())
    with bind_chat_execution(tmp_path, "key", generation):
        _spawn(review._agent)
        review.gate.set()
        assert review.finished.wait(_WAIT_S)
        assert review.outcome == [False]  # no identity inside the review: nothing to observe
        assert observe_tool_before_execution() is True
        assert store.worker_finished("key", generation)
        with pytest.raises(RuntimeError, match="Durable tool observation refused"):
            observe_tool_before_execution()


def test_stop_still_cancels_a_review_that_outlives_its_turn(tmp_path, monkeypatch):
    store, generation = _started_turn(tmp_path)
    review = _Review(_bare_agent(), monkeypatch, lambda: pytest.fail("a cancelled review must not run tools"))
    agent = review._agent
    with bind_chat_execution(tmp_path, "key", generation):
        _spawn(agent)
    assert store.worker_finished("key", generation)
    run = agent._background_review_run
    assert run is not None

    releaser = threading.Thread(target=lambda: (run.cancel_requested.wait(_WAIT_S), review.gate.set()))
    releaser.start()
    cancel_background_review_for_live_turn(agent)
    releaser.join(_WAIT_S)

    assert review.finished.wait(_WAIT_S)
    assert review.outcome == ["cancelled"]
    assert run.cancel_requested.is_set() and run.request_done.is_set()
