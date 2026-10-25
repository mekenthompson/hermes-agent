"""Pre-dispatch policy refusals must not consume an unbounded inference loop."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.agent.test_tool_call_guardrail_runtime import (
    _hard_stop_config,
    _make_agent,
    _mock_response,
    _mock_tool_call,
)


@pytest.mark.parametrize("executor", ["sequential", "concurrent"])
def test_repeated_plugin_refusals_halt_without_dispatch_or_mutation(executor):
    agent = _make_agent("delegate_task", config=_hard_stop_config())
    agent._stall_guards = False  # Failure accounting must not depend on result deduplication.
    refusal = "pre_tool_call plugin callback timed out or is still running"
    messages = []
    progress = []
    agent.tool_progress_callback = lambda *args, **kwargs: progress.append(args)
    args = {"tasks": [{"goal": "Read the CI status"}]}

    with (
        patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", return_value=(refusal, None)),
        patch("model_tools.handle_function_call") as dispatch,
        patch.object(agent, "_record_file_mutation_result") as record_mutation,
    ):
        for attempt in range(4):
            call = _mock_tool_call("delegate_task", json.dumps(args), f"refused-{attempt}")
            getattr(agent, f"_execute_tool_calls_{executor}")(
                SimpleNamespace(content="", tool_calls=[call]), messages, "refusal-test",
            )
            if agent._tool_guardrail_halt_decision is not None:
                break

    dispatch.assert_not_called()
    record_mutation.assert_not_called()
    assert progress == []
    assert len(messages) == 2
    assert all(refusal in message["content"] for message in messages)
    assert agent._tool_guardrail_halt_decision.should_halt
    # Denied calls are failures, not spawned children or successful mutations.
    assert agent._tool_guardrails._turn_subagent_count == 0


@pytest.mark.parametrize("hard_stop", [False, True])
def test_policy_refusal_stops_model_retries_only_when_enabled(hard_stop):
    agent = _make_agent("web_search", config=_hard_stop_config(hard_stop_enabled=hard_stop))
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="", finish_reason="tool_calls", tool_calls=[
            _mock_tool_call("web_search", '{"query":"same"}', f"refused-{index}"),
        ]) for index in range(3)
    ] + [_mock_response(content="The policy refused the search.")]
    with (
        patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", return_value=("policy refused", None)),
        patch("model_tools.handle_function_call") as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("Check the status")

    dispatch.assert_not_called()
    assert agent.client.chat.completions.create.call_count == (2 if hard_stop else 4)
    assert (result["turn_exit_reason"] == "guardrail_halt") is hard_stop
    contents = [m["content"] for m in result["messages"] if m["role"] == "tool"]
    assert all("policy refused" in content for content in contents)
    assert "rejected calls did not execute" in contents[-1]
    assert agent._tool_guardrails._turn_web_search_count == 0


def test_varied_refused_terminal_commands_are_not_diagnostic_execution():
    from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController

    guard = ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=True, same_tool_failure_halt_after=3,
    ))
    for index in range(3):
        decision = guard.after_refusal("terminal", {"command": f"command-{index}"}, '{"error":"refused"}')
        assert decision.should_halt is (index == 2)
    assert decision.code == "same_tool_failure_halt"
    assert "bypass" in decision.message


def test_guardrail_synthetic_block_does_not_count_itself_as_another_refusal():
    agent = _make_agent("web_search", config=_hard_stop_config())
    args = {"query": "same"}
    for _ in range(2):
        agent._tool_guardrails.after_call("web_search", args, '{"error":"failed"}', failed=True)
    call = _mock_tool_call("web_search", json.dumps(args), "guardrail-block")
    with (
        patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", return_value=(None, None)),
        patch.object(agent._tool_guardrails, "after_refusal", wraps=agent._tool_guardrails.after_refusal) as observe,
        patch("model_tools.handle_function_call") as dispatch,
    ):
        agent._execute_tool_calls_sequential(SimpleNamespace(tool_calls=[call]), [], "refusal-test")
    dispatch.assert_not_called()
    observe.assert_not_called()
