"""A lost native request response must not create another work unit or follow-up."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_concurrent_creation_retries_share_one_task(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    barrier = threading.Barrier(2)
    original = kb._new_task_id

    def allocate():
        result = original()
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(kb, "_new_task_id", allocate)

    def create():
        with kc.connect_closing() as conn:
            return kb.create_task(conn, title="remaining work", assignee="default", idempotency_key="request-1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        tasks = [future.result(timeout=20) for future in futures]
    assert tasks[0] == tasks[1]


def test_lost_creation_response_stays_the_same_after_archive(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with kc.connect_closing() as conn:
        task = kb.create_task(conn, title="work", assignee="default", idempotency_key="request-1")
        kb.archive_task(conn, task)
        assert kb.create_task(conn, title="work", assignee="default", idempotency_key="request-1") == task
        assert kb.get_task(conn, task).status == "archived"


def test_followup_retries_are_atomic_and_conflicts_are_refused(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with kc.connect_closing() as conn:
        task = kb.create_task(conn, title="work", assignee="default", initial_status="blocked")
    barrier = threading.Barrier(2)

    def submit():
        with kc.connect_closing() as conn:
            barrier.wait(timeout=10)
            return kb.add_comment(conn, task, "default", "Keep the existing checkpoint", idempotency_key="followup-1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        comments = [future.result(timeout=20) for future in futures]
    assert comments[0] == comments[1]
    with kc.connect_closing() as conn:
        assert len(kb.list_comments(conn, task)) == 1
        assert kb.get_task(conn, task).status == "blocked"
        with pytest.raises(ValueError, match="idempotency"):
            kb.add_comment(conn, task, "default", "Changed instruction", idempotency_key="followup-1")
