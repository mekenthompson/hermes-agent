"""Gateway session key and Hermes session id are different cwd keys.

Messaging gateways record the shell cwd under the platform session key.
Worktree creation used to read it under task_id, which the gateway sets to
the Hermes session id. The lookup missed, fell back to TERMINAL_CWD, and
admission rejected the writer. This is every gateway profile, not one agent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.admission_contract import AdmissionCaller
from hermes_cli.admission_runtime import admit_writer, release_admission
from tools.approval_context import reset_current_session_key, set_current_session_key
from tools.delegate_tool_child_run import _create_isolated_worktree
from tools.terminal_tool import (
    clear_session_cwd,
    get_session_cwd,
    record_session_cwd,
)


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "test@test"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["update-ref", "refs/remotes/origin/main", "HEAD"], repo)
    return repo


def _parent(fallback: Path) -> SimpleNamespace:
    return SimpleNamespace(
        cwd=str(fallback),
        terminal_cwd=str(fallback),
        _subdirectory_hints=SimpleNamespace(working_dir=str(fallback)),
    )


@pytest.fixture
def cwd_keys():
    keys = ["agent:main:slack:channel", "20260924_000000_session"]
    for key in keys:
        clear_session_cwd(key)
    yield keys
    for key in keys:
        clear_session_cwd(key)


def test_falsifier_session_id_does_not_see_session_key_record(tmp_path, cwd_keys):
    """Prove the suspect before relying on the worktree assertion.

    get_session_cwd(session_id) must miss when the terminal recorded under
    the gateway session key. If this is false, the key-split suspect is wrong.
    """
    session_key, session_id = cwd_keys
    repo = _make_repo(tmp_path)
    record_session_cwd(session_key, str(repo))

    assert get_session_cwd(session_id) is None
    assert get_session_cwd(session_key) == str(repo)


def test_worktree_creation_reads_gateway_session_key_not_session_id(
    tmp_path, cwd_keys, monkeypatch
):
    """Fails if worktree creation reads session_id while cwd was recorded under session_key."""
    session_key, session_id = cwd_keys
    repo = _make_repo(tmp_path)
    fallback = tmp_path / "not-a-repo"
    fallback.mkdir()
    record_session_cwd(session_key, str(repo))
    monkeypatch.setenv("TERMINAL_CWD", str(fallback))
    monkeypatch.setattr(
        "tools.delegate_tool._get_worktree_isolation", lambda: True
    )
    monkeypatch.setattr(
        "tools.subagent_worktree.local_backend_active", lambda: True
    )
    token = set_current_session_key(session_key)
    try:
        info = _create_isolated_worktree(_parent(fallback), session_id, "child-1")
    finally:
        reset_current_session_key(token)

    assert info is not None, "write-capable child got no isolated workspace"
    assert Path(info["repo_root"]).resolve() == repo.resolve()
    assert Path(info["path"]).resolve() != fallback.resolve()
    git_dir = subprocess.run(
        ["git", "-C", info["path"], "rev-parse", "--git-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    common = subprocess.run(
        ["git", "-C", info["path"], "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert Path(git_dir).resolve() != Path(common).resolve()


def test_session_that_never_entered_a_repo_fails_closed(tmp_path, monkeypatch):
    fallback = tmp_path / "not-a-repo"
    fallback.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(fallback))
    monkeypatch.setattr(
        "tools.delegate_tool._get_worktree_isolation", lambda: True
    )
    monkeypatch.setattr(
        "tools.subagent_worktree.local_backend_active", lambda: True
    )
    token = set_current_session_key("agent:main:telegram:chat")
    try:
        info = _create_isolated_worktree(
            _parent(fallback), "20260924_000001_norepo", "child-closed"
        )
    finally:
        reset_current_session_key(token)

    assert info is None
    assert not (fallback / ".worktrees").exists()


def test_task_id_record_still_works_without_a_gateway_session_key(
    tmp_path, cwd_keys, monkeypatch
):
    """CLI and cron record under task_id when no gateway session key is set."""
    _, session_id = cwd_keys
    repo = _make_repo(tmp_path)
    fallback = tmp_path / "not-a-repo"
    fallback.mkdir()
    record_session_cwd(session_id, str(repo))
    monkeypatch.setenv("TERMINAL_CWD", str(fallback))
    monkeypatch.setattr(
        "tools.delegate_tool._get_worktree_isolation", lambda: True
    )
    monkeypatch.setattr(
        "tools.subagent_worktree.local_backend_active", lambda: True
    )
    info = _create_isolated_worktree(_parent(fallback), session_id, "child-cli")

    assert info is not None
    assert Path(info["repo_root"]).resolve() == repo.resolve()


def test_admitted_writer_worktree_is_the_cd_repo(
    tmp_path, cwd_keys, monkeypatch
):
    session_key, session_id = cwd_keys
    repo = _make_repo(tmp_path)
    fallback = tmp_path / "not-a-repo"
    fallback.mkdir()
    ledger = tmp_path / "admission.db"
    record_session_cwd(session_key, str(repo))
    monkeypatch.setenv("TERMINAL_CWD", str(fallback))
    monkeypatch.setenv("HERMES_ADMISSION_LEDGER", str(ledger))
    monkeypatch.setattr(
        "tools.delegate_tool._get_worktree_isolation", lambda: True
    )
    monkeypatch.setattr(
        "tools.subagent_worktree.local_backend_active", lambda: True
    )
    token = set_current_session_key(session_key)
    try:
        info = _create_isolated_worktree(_parent(fallback), session_id, "child-admit")
    finally:
        reset_current_session_key(token)

    assert info is not None
    admission = admit_writer(
        request_id="delegate:child-admit",
        caller=AdmissionCaller.DELEGATE,
        workspace=info["path"],
        priority=0,
        writer_id="delegate:child-admit",
        delegate_cap=1,
    )
    try:
        assert admission.state == "running", admission.reason
        assert Path(info["repo_root"]).resolve() == repo.resolve()
    finally:
        release_admission(admission.lease_id)


def test_resolver_prefers_task_record_then_gateway_session_key(tmp_path, cwd_keys):
    from tools.terminal_tool import resolve_recorded_session_cwd

    session_key, session_id = cwd_keys
    repo = tmp_path / "recorded"
    repo.mkdir()
    other = tmp_path / "task-id-only"
    other.mkdir()
    record_session_cwd(session_key, str(repo))
    record_session_cwd(session_id, str(other))
    token = set_current_session_key(session_key)
    try:
        assert resolve_recorded_session_cwd(session_id) == str(other)
        clear_session_cwd(session_id)
        assert resolve_recorded_session_cwd(session_id) == str(repo)
    finally:
        reset_current_session_key(token)

    record_session_cwd(session_id, str(other))
    assert resolve_recorded_session_cwd(session_id) == str(other)


def test_sibling_readers_use_the_gateway_session_key(tmp_path, cwd_keys, monkeypatch):
    session_key, session_id = cwd_keys
    repo = tmp_path / "cd-target"
    repo.mkdir()
    plain = tmp_path / "configured"
    plain.mkdir()
    record_session_cwd(session_key, str(repo))
    monkeypatch.setenv("TERMINAL_CWD", str(plain))
    token = set_current_session_key(session_key)
    try:
        from tools.code_execution_env import _resolve_child_cwd
        from tools.file_tools_paths import _authoritative_workspace_root

        assert _authoritative_workspace_root(session_id) == str(repo)
        assert _resolve_child_cwd("project", str(plain), session_id) == str(repo)
    finally:
        reset_current_session_key(token)


def test_child_worktree_wins_over_inherited_gateway_key(tmp_path, cwd_keys, monkeypatch):
    from tools.terminal_tool import _plan_execution, record_execution_cwd, register_container_alias

    session_key, _session_id = cwd_keys
    parent_repo = tmp_path / "parent-repo"
    worktree = tmp_path / "child-worktree"
    moved = tmp_path / "child-cd"
    parent_repo.mkdir()
    worktree.mkdir()
    moved.mkdir()
    child_id = "subagent-0-abc"
    record_session_cwd(session_key, str(parent_repo))
    record_session_cwd(child_id, str(worktree))
    register_container_alias(child_id, "parent-session")
    monkeypatch.setenv("TERMINAL_CWD", str(parent_repo))
    token = set_current_session_key(session_key)
    try:
        from tools.file_tools_paths import _authoritative_workspace_root

        assert _authoritative_workspace_root(child_id) == str(worktree)
        plan = _plan_execution("pwd", task_id=child_id, timeout=30, background=False, _host_local=False)
        assert plan.cwd == str(worktree)
        record_execution_cwd(child_id, str(moved))
        assert get_session_cwd(session_key) == str(parent_repo)
        assert get_session_cwd(child_id) == str(moved)
    finally:
        reset_current_session_key(token)
        from tools.terminal_tool import _container_alias_lock, _container_aliases
        with _container_alias_lock:
            _container_aliases.pop(child_id, None)


def test_child_terminal_tool_runs_in_its_worktree(tmp_path, cwd_keys, monkeypatch):
    import json

    from tools.terminal_tool import register_container_alias, terminal_tool

    session_key, _session_id = cwd_keys
    parent = tmp_path / "parent"
    worktree = tmp_path / "wt"
    (parent / "only-parent").mkdir(parents=True)
    (worktree / "sub").mkdir(parents=True)
    child_id = "subagent-0-pwd"
    record_session_cwd(session_key, str(parent))
    record_session_cwd(child_id, str(worktree))
    register_container_alias(child_id, "parent-session")
    monkeypatch.setenv("TERMINAL_CWD", str(parent))
    token = set_current_session_key(session_key)
    try:
        first = json.loads(terminal_tool("pwd", task_id=child_id))
        assert first["exit_code"] == 0
        assert first["output"].strip() == str(worktree)
        moved = json.loads(terminal_tool("cd sub && pwd", task_id=child_id))
        assert moved["exit_code"] == 0
        assert moved["output"].strip() == str(worktree / "sub")
        assert get_session_cwd(session_key) == str(parent)
        assert get_session_cwd(child_id) == str(worktree / "sub")
    finally:
        reset_current_session_key(token)
        from tools.terminal_tool import _container_alias_lock, _container_aliases
        with _container_alias_lock:
            _container_aliases.pop(child_id, None)


