"""Real filesystem integration coverage for the live admission adapters."""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from hermes_cli.admission import AdmissionController, AdmissionLimits
from hermes_cli.admission_contract import AdmissionCaller, AdmissionErrorCode
from hermes_cli.admission_runtime import admit_writer, ledger_path, release_admission


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(path), *args], text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def _worktrees(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("base\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "base")
    _git(repo, "remote", "add", "origin", str(repo))
    # The adapter deliberately requires an advertised main-line base.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    first, second, third, fourth = (tmp_path / name for name in ("first", "second", "third", "fourth"))
    _git(repo, "worktree", "add", "-b", "worker/first", str(first), "HEAD")
    _git(repo, "worktree", "add", "-b", "worker/second", str(second), "HEAD")
    _git(repo, "worktree", "add", "-b", "worker/third", str(third), "HEAD")
    _git(repo, "worktree", "add", "-b", "worker/fourth", str(fourth), "HEAD")
    return repo, first, second, third, fourth


def _admit(request_id: str, caller: AdmissionCaller, workspace: Path, *, priority: int = 0, **kwargs):
    return admit_writer(
        request_id=request_id,
        caller=caller,
        workspace=str(workspace),
        priority=priority,
        writer_id=f"{caller.value}:{request_id}",
        kanban_cap=2,
        delegate_cap=3,
        **kwargs,
    )


def test_temp_home_shares_one_ledger_for_kanban_and_delegate_capacity_priority_and_writers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _repo, first, second, third, fourth = _worktrees(tmp_path)

    kanban = _admit("kanban-1", AdmissionCaller.KANBAN, first, priority=10)
    delegate = _admit("delegate-1", AdmissionCaller.DELEGATE, second, priority=20)
    queued_low = _admit("low", AdmissionCaller.KANBAN, third, priority=10)
    queued_high = _admit("high", AdmissionCaller.DELEGATE, fourth, priority=90)

    assert kanban.state == delegate.state == "running"
    assert queued_low.state == queued_high.state == "queued"
    assert queued_low.reason == "capacity"
    assert release_admission(kanban.lease_id) is None

    connection = sqlite3.connect(ledger_path(), isolation_level=None)
    try:
        controller = AdmissionController(connection, AdmissionLimits(running={"gateway": 2}, queued={"gateway": 2}))
        promoted = controller.promote_next()
        assert promoted is not None
        assert promoted.request_id == "high"
        assert promoted.state == "running"
    finally:
        connection.close()

    # Both callers used the same common directory but distinct branch targets;
    # non-overlapping writers are valid.  An exact branch overlap is rejected.
    conflict = _admit("same-branch", AdmissionCaller.KANBAN, second)
    assert conflict.state == "rejected"
    assert conflict.reason == AdmissionErrorCode.CONFLICTING_WRITER.value


def test_temp_home_rejects_unisolated_delegate_and_records_durable_reason(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    rejected = admit_writer(
        request_id="delegate-no-worktree", caller=AdmissionCaller.DELEGATE, workspace=None,
        priority=0, writer_id="delegate:delegate-no-worktree", kanban_cap=2, delegate_cap=2,
    )
    assert rejected.state == "rejected"
    assert rejected.reason == AdmissionErrorCode.UNSUPPORTED_PATH.value
    connection = sqlite3.connect(ledger_path())
    try:
        row = connection.execute(
            "SELECT caller, reason FROM admission_rejections WHERE request_id = ?", ("delegate-no-worktree",)
        ).fetchone()
    finally:
        connection.close()
    assert row == ("delegate", AdmissionErrorCode.UNSUPPORTED_PATH.value)


def test_profile_scope_a_b_a_uses_each_runtime_home_ledger(tmp_path, monkeypatch):
    _repo, first, second, _third, _fourth = _worktrees(tmp_path)
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    home_a.mkdir()
    home_b.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    a_first = _admit("a-first", AdmissionCaller.KANBAN, first)
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    b_first = _admit("b-first", AdmissionCaller.DELEGATE, second)
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    a_second = _admit("a-second", AdmissionCaller.DELEGATE, second)

    assert a_first.state == b_first.state == "running"
    assert a_second.state == "running"
    assert ledger_path() == home_a / "admission.db"
    assert (home_b / "admission.db").exists()


def test_dependencies_do_not_consume_running_capacity(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _repo, first, second, _third, _fourth = _worktrees(tmp_path)
    blocked = _admit(
        "blocked", AdmissionCaller.KANBAN, first,
        dependencies=("parent",), unresolved_dependencies=("parent",),
    )
    admitted = _admit("ready", AdmissionCaller.DELEGATE, second)

    assert blocked.state == "queued"
    assert blocked.reason == AdmissionErrorCode.DEPENDENCIES_UNSATISFIED.value
    assert admitted.state == "running"
