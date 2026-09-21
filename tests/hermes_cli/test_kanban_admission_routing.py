"""Kanban worker admission-route coverage."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import admission_contract
from hermes_cli import admission_runtime
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

from hermes_cli.admission import Admission
from hermes_cli.admission_contract import AdmissionErrorCode, AdmissionResult


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as connection:
        yield connection


def test_rejected_kanban_route_happens_before_claim_or_spawn(conn, monkeypatch):
    task_id = kb.create_task(conn, title="must not launch", assignee="default")
    seen: list[tuple[str, str]] = []

    def reject(path, _caller, *, request_id):
        seen.append((path, request_id))
        return AdmissionResult(
            request_id=request_id,
            state="rejected",
            reason=AdmissionErrorCode.UNSUPPORTED_PATH.value,
        )

    monkeypatch.setattr(admission_contract, "route_launch_path", reject)
    result = kbd.dispatch_once(
        conn,
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("spawn must follow admission"),
    )

    assert seen == [("kanban.dispatch_lane", task_id)]
    assert result.spawned == []
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "ready"


def test_kanban_route_precedes_the_parallel_writer(conn, monkeypatch):
    task_id = kb.create_task(conn, title="admitted", assignee="default")
    order: list[str] = []
    original_route = admission_contract.route_launch_path

    def record_route(path, caller, *, request_id):
        order.append("route")
        return original_route(path, caller, request_id=request_id)

    def spawn(*_args, **_kwargs):
        order.append("spawn")
        return 4242

    monkeypatch.setattr(admission_contract, "route_launch_path", record_route)
    result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert [entry[0] for entry in result.spawned] == [task_id]
    assert order == ["route", "spawn"]


def test_queued_kanban_admission_is_cancelled_before_spawn_retry(conn, monkeypatch):
    task_id = kb.create_task(conn, title="queued admission", assignee="default")
    cancelled: list[str] = []

    monkeypatch.setattr(
        admission_runtime,
        "admit_writer",
        lambda **kwargs: Admission(
            kwargs["request_id"], "kanban:test", "queued", {"gateway": 1}, True, None, None, "capacity",
        ),
    )
    monkeypatch.setattr(admission_runtime, "cancel_admission_request", lambda request_id: cancelled.append(request_id) or True)
    monkeypatch.setattr(kbd, "_default_spawn", lambda *_args, **_kwargs: pytest.fail("spawn must not follow queued admission"))

    result = kbd.dispatch_once(conn, max_in_progress=1)

    assert result.spawned == []
    assert len(cancelled) == 1
    assert cancelled[0].startswith(f"kanban:{task_id}:run:")


def test_direct_worker_helper_rejects_before_popen(monkeypatch, tmp_path):
    task = kb.Task(
        id="t_direct_worker",
        title="direct worker",
        body=None,
        assignee="default",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
    )
    monkeypatch.setattr(
        admission_contract,
        "route_launch_path",
        lambda _path, _caller, *, request_id: AdmissionResult(
            request_id=request_id,
            state="rejected",
            reason=AdmissionErrorCode.UNSUPPORTED_PATH.value,
        ),
    )
    monkeypatch.setattr(kbd.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Popen must not run"))

    with pytest.raises(RuntimeError, match="admission path rejected: unsupported_path"):
        kbd._default_spawn(task, str(tmp_path))
