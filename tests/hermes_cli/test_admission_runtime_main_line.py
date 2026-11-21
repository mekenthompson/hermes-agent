"""Delegate admission anchors a worktree to the main line that actually shares its history."""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from hermes_cli.admission_contract import AdmissionCaller, AdmissionErrorCode
from hermes_cli.admission_runtime import admit_writer, ledger_path, release_admission


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], text=True, capture_output=True, check=True).stdout.strip()


def _repo(path: Path, content: str) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    (path / "README").write_text(content)
    _git(path, "add", "README")
    _git(path, "commit", "-m", content.strip())
    return path


def _worker_with_unrelated_upstream(tmp_path: Path, *, related_origin: bool) -> Path:
    """A fleet-style checkout whose ``upstream`` remote is a different project."""
    repo = _repo(tmp_path / "repo", "fleet\n")
    unrelated = _repo(tmp_path / "unrelated", "core\n")
    _git(repo, "fetch", "-q", str(unrelated), "main:refs/remotes/upstream/main")
    if related_origin:
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    worker = tmp_path / "worker"
    _git(repo, "worktree", "add", "-b", "worker/one", str(worker), "HEAD")
    return worker


def _admit(request_id: str, worker: Path):
    return admit_writer(
        request_id=request_id, caller=AdmissionCaller.DELEGATE, workspace=str(worker),
        priority=0, writer_id=f"delegate:{request_id}", delegate_cap=2,
    )


def test_unrelated_upstream_falls_back_to_related_origin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    worker = _worker_with_unrelated_upstream(tmp_path, related_origin=True)

    admission = _admit("delegate-fleet", worker)

    assert admission.state == "running", admission.reason
    release_admission(admission.lease_id)


def test_no_related_main_line_is_rejected_with_a_readable_reason(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    worker = _worker_with_unrelated_upstream(tmp_path, related_origin=False)

    admission = _admit("delegate-orphan", worker)

    assert admission.state == "rejected"
    assert admission.reason == AdmissionErrorCode.UNSUPPORTED_PATH.value
    connection = sqlite3.connect(ledger_path())
    try:
        (detail,) = connection.execute(
            "SELECT detail FROM admission_rejections WHERE request_id = ?", ("delegate-orphan",)
        ).fetchone()
    finally:
        connection.close()
    assert detail == "workspace shares no history with upstream/main"
