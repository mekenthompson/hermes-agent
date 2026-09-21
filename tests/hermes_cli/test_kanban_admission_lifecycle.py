"""Kanban worker admission is retained only while its exact run is live."""
from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import admission_runtime as ar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.admission_contract import AdmissionCaller


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("base\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "base")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    worker = tmp_path / "worker"
    _git(repo, "worktree", "add", "-b", "worker/lifecycle", str(worker), "HEAD")
    return worker


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    worker = _worktree(tmp_path)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="admission lifecycle", assignee="default")
        task = kb.claim_task(conn, task_id)
        assert task is not None and task.current_run_id is not None
        admission = ar.admit_writer(
            request_id=ar.kanban_request_id(task_id, task.current_run_id),
            caller=AdmissionCaller.KANBAN,
            workspace=str(worker),
            priority=0,
            writer_id=f"kanban:{task_id}",
            kanban_cap=1,
        )
        assert admission.state == "running"
        yield conn, task_id, task.current_run_id


def _admission_row() -> tuple[str, int | None]:
    with sqlite3.connect(ar.ledger_path()) as conn:
        row = conn.execute(
            "SELECT state, lease_expires_at FROM admission_requests ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_normal_completion_releases_once(lifecycle):
    conn, task_id, run_id = lifecycle

    assert kb.complete_task(conn, task_id, summary="done", expected_run_id=run_id)
    assert _admission_row()[0] == "released"
    assert not ar.release_kanban_admission(task_id, run_id)


def test_heartbeat_renews_without_premature_release(lifecycle, monkeypatch):
    conn, task_id, run_id = lifecycle
    with sqlite3.connect(ar.ledger_path(), isolation_level=None) as ledger:
        created_at, old_expiry = ledger.execute(
            "SELECT created_at, lease_expires_at FROM admission_requests"
        ).fetchone()
    monkeypatch.setattr(ar.time, "time", lambda: int(created_at) + 10)

    assert kbd.heartbeat_worker(conn, task_id, expected_run_id=run_id)
    state, renewed_expiry = _admission_row()
    assert state == "running"
    assert renewed_expiry == int(old_expiry) + 10


def test_dead_worker_reclaim_releases_reservation(lifecycle, monkeypatch):
    conn, task_id, _run_id = lifecycle
    task = kb.get_task(conn, task_id)
    assert task is not None
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = ? WHERE id = ?",
        (987654, int(time.time()) - 120, task_id),
    )
    monkeypatch.setattr(kbd, "_worker_alive", lambda *_args: False)

    assert kbd._reclaim_dead_workers(conn).crashed == [task_id]
    assert _admission_row()[0] == "released"
