"""Live operator-note injection into a running kanban worker.

``tools.kanban_tools.inject_new_comments_from_env`` polls the worker's task
for comments added *after* the run started and folds them into the live turn
via the agent's OUT-OF-BAND steer channel — so a user can talk to a running
task without the block→comment→unblock dance or a restart.

Verifies: no-op off a worker, watermark seeding (history isn't re-injected),
new comments steer, and own-authored comments are skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
import tools.kanban_tools as kt


class FakeAgent:
    def __init__(self):
        self.steers: list[str] = []

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return True


@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    # Reset module-level poll state so tests don't leak into each other.
    kt._comment_watermark.clear()
    kt._comment_poll_last_attempt = 0.0
    return home


def _unthrottle():
    """Bypass the inter-poll rate limit for deterministic tests."""
    kt._comment_poll_last_attempt = 0.0


def test_noop_without_worker_env(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = FakeAgent()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_seed_then_inject_new_comment(worker_home, monkeypatch):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="live task")
        kb.add_comment(conn, tid, author="desktop", body="pre-existing note")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    agent = FakeAgent()

    # First poll seeds the watermark past the existing thread — no injection.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []

    conn = kbc.connect()
    try:
        kb.add_comment(conn, tid, author="desktop", body="actually use the v2 API")
    finally:
        conn.close()

    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    assert len(agent.steers) == 1
    assert "v2 API" in agent.steers[0]

    # Watermark advanced — a re-poll with no new comments injects nothing.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert len(agent.steers) == 1


def test_skips_own_authored_comments(worker_home, monkeypatch):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="echo guard")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    agent = FakeAgent()

    _unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed

    conn = kbc.connect()
    try:
        kb.add_comment(conn, tid, author="worker-bot", body="i did a thing")
    finally:
        conn.close()

    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_delegated_child_in_worker_process_neither_receives_nor_consumes_notes(worker_home, monkeypatch):
    """A delegate_task child inherits the worker's ``HERMES_KANBAN_TASK``; operator notes
    address the worker, so the child must not be steered by them and must not advance the
    shared watermark (which would make the worker miss them) (#112817)."""
    from agent.delegation_context import delegated_child_context

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="live task")
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")

    worker = FakeAgent()
    _unthrottle()
    assert kt.inject_new_comments_from_env(worker) is False  # seed

    conn = kbc.connect()
    try:
        kb.add_comment(conn, tid, author="desktop", body="operator note for the worker")
    finally:
        conn.close()

    child = FakeAgent()
    with delegated_child_context():
        _unthrottle()
        assert kt.inject_new_comments_from_env(child) is False
    assert child.steers == []

    _unthrottle()
    assert kt.inject_new_comments_from_env(worker) is True
    assert "operator note" in worker.steers[0]


def test_skips_own_authored_comments_without_env_profile(worker_home, monkeypatch):
    """Own comments are skipped even when ``HERMES_PROFILE`` is absent: the
    injection filter resolves the worker's identity the same way the persisted
    write side does (env → ``HERMES_HOME``-derived active profile), so a worker's
    own notes can never steer its live turn as fake operator messages."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="identity echo guard")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    agent = FakeAgent()

    _unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed

    identity = kt._persisted_identity()
    assert identity != "worker"  # resolved from HERMES_HOME, not the generic label

    conn = kbc.connect()
    try:
        kb.add_comment(conn, tid, author=identity, body="note to self")
    finally:
        conn.close()

    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_same_profile_chat_followups_reach_existing_worker_once(worker_home, monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from tools.registry import registry

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="one continuing work unit", assignee="worker-bot")
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    worker = FakeAgent()
    assert not kt.inject_new_comments_from_env(worker)
    monkeypatch.delenv("HERMES_KANBAN_TASK")

    def followup(index):
        return json.loads(registry.dispatch("kanban_comment", {"task_id": tid,
            "body": f"follow-up {index}", "idempotency_key": f"request-{index}"}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(followup, [1, 2]))
    assert all(receipt["ok"] for receipt in receipts)
    assert followup(1)["comment_id"] == receipts[0]["comment_id"]
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    _unthrottle()
    assert kt.inject_new_comments_from_env(worker)
    assert len(worker.steers) == 1
    assert all(worker.steers[0].count(f"follow-up {index}") == 1 for index in (1, 2))
    _unthrottle()
    assert not kt.inject_new_comments_from_env(worker)
    with kbc.connect() as conn:
        assert len(kb.list_comments(conn, tid)) == 2
        assert kb.get_task(conn, tid).status == "ready"


def test_refused_native_steer_does_not_consume_followup(worker_home, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="continuation delivery")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    worker = FakeAgent()
    assert not kt.inject_new_comments_from_env(worker)
    with kbc.connect() as conn:
        kb.add_comment(conn, tid, "desktop", "preserve this instruction", idempotency_key="request")
    original = worker.steer
    worker.steer = lambda text: False
    _unthrottle()
    assert not kt.inject_new_comments_from_env(worker)
    worker.steer = original
    _unthrottle()
    assert kt.inject_new_comments_from_env(worker)
    assert len(worker.steers) == 1
    assert "preserve this instruction" in worker.steers[0]
