from unittest.mock import MagicMock, patch

from tools.approval_context import approval_task_context
from tools.approval_smart import _smart_approve


def _response(answer="ESCALATE"):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = answer
    return response


def test_smart_review_receives_current_user_task_scope_separately_from_command():
    with patch("agent.auxiliary_client.call_llm", return_value=_response()) as call_llm, \
         patch("tools.approval_context._get_approval_config", return_value={}):
        with approval_task_context(
            "Edit only src/cli.py; do not change tests or dependencies.",
            identity="desktop:user-7", profile="profile-a", session="session-2",
            task_id="task-9", run_id="run-3",
        ):
            assert _smart_approve("python -c 'print(1)'", "script execution") == "escalate"

    messages = call_llm.call_args.kwargs["messages"]
    assert "src/cli.py" in messages[1]["content"]
    assert "do not change tests or dependencies" in messages[1]["content"]
    assert "desktop:user-7" in messages[1]["content"]
    assert "profile-a" in messages[1]["content"]
    assert "session-2" in messages[1]["content"]
    assert "task-9" in messages[1]["content"]
    assert "run-3" in messages[1]["content"]
    assert "UNTRUSTED USER TASK CONTEXT" in messages[1]["content"]
    assert "<command>" in messages[1]["content"]


def test_smart_review_does_not_retain_task_context_after_scope_exits():
    with patch("agent.auxiliary_client.call_llm", return_value=_response()) as call_llm, \
         patch("tools.approval_context._get_approval_config", return_value={}):
        with approval_task_context("only this task", identity="u", profile="p", session="s"):
            pass
        _smart_approve("echo ok", "script execution")

    assert "only this task" not in call_llm.call_args.kwargs["messages"][1]["content"]
