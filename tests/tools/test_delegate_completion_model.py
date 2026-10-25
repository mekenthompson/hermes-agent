"""The async completion header names the model the children actually run on."""

from dataclasses import fields
from types import SimpleNamespace

from tools import async_delegation
from tools.delegate_tool_dispatch import _Batch, _dispatch_unit
from tools.approval_context import approval_task_context


def _unit(configured_model):
    batch = _Batch(**{field.name: None for field in fields(_Batch)})
    task = {"goal": "probe"}
    batch.task_list, batch.live_writers, batch.top_role = [task], [], "leaf"
    batch.children = [(0, task, SimpleNamespace(model="inherited/model"))]
    batch.creds = {"model": configured_model}
    return batch


def test_completion_model_falls_back_to_inherited_child_model(monkeypatch):
    seen = []
    def dispatch(**kw):
        seen.append(kw["model"])
        return {"status": "dispatched"}

    monkeypatch.setattr(async_delegation, "dispatch_async_delegation_batch", dispatch)
    with approval_task_context("work", identity="fixture", profile="fixture", session="fixture",
                               task_id="fixture", run_id="1", is_current=lambda: True):
        _dispatch_unit(_unit(None), None, None, {})  # blank: children inherit the parent's model
        _dispatch_unit(_unit("configured/model"), None, None, {})
    assert seen == ["inherited/model", "configured/model"]
