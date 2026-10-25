"""HF-562: native lifecycle request receipts fence retries across later Stop/resume."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from threading import Barrier

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def database(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return kb.init_db()


def _create(conn, operation):
    return kb.create_task(conn, title="durable instruction", assignee="worker",
                          initial_status="blocked" if operation == "unblock" else "running")


def _call(conn, tid, operation, **kwargs):
    return getattr(kb, operation + "_task")(conn, tid, **kwargs)


@pytest.mark.parametrize("operation", ["block", "unblock"])
def test_lost_response_process_restart_and_later_opposite_transition(database, operation):
    with kbc.connect(database) as conn:
        tid = _create(conn, operation)
    # The process commits the real native mutation, then dies before the caller
    # can receive its result. A restarted client uses the same request key.
    source = """
import os, sys
from pathlib import Path
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
with kbc.connect(Path(sys.argv[1])) as conn:
    assert getattr(kb, sys.argv[3] + '_task')(conn, sys.argv[2], idempotency_key='lost-response')
os._exit(19)
"""
    child = subprocess.run([sys.executable, "-c", source, str(database), tid, operation],
                           cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=30,
                           check=False)
    assert child.returncode == 19, child.stderr.decode()
    with kbc.connect(database) as conn:
        opposite = "unblock" if operation == "block" else "block"
        assert _call(conn, tid, opposite)
        before = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        runs = kb.list_runs(conn, tid)
        assert kb.lifecycle_request_received(conn, tid, operation, "lost-response") is True
        assert _call(conn, tid, operation, idempotency_key="lost-response") is True
        assert kb.get_task(conn, tid) == before
        assert kb.list_events(conn, tid) == events
        assert kb.list_runs(conn, tid) == runs


@pytest.mark.parametrize("operation", ["block", "unblock"])
def test_concurrent_same_request_changes_native_state_once(database, operation):
    with kbc.connect(database) as conn:
        tid = _create(conn, operation)
        before = len(kb.list_events(conn, tid))
    gate = Barrier(2)

    def attempt():
        with kbc.connect(database) as conn:
            gate.wait(timeout=10)
            return _call(conn, tid, operation, idempotency_key="same-request")

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(attempt) for _ in range(2)]
        assert [job.result(timeout=20) for job in attempts] == [True, True]
    with kbc.connect(database) as conn:
        added = kb.list_events(conn, tid)[before:]
        assert [event.kind for event in added] == ["blocked" if operation == "block" else "unblocked",
                                                   "lifecycle_request"]
        assert len(kb.list_runs(conn, tid)) == 0


def test_key_reuse_with_different_instruction_or_operation_refuses(database):
    with kbc.connect(database) as conn:
        tid = _create(conn, "block")
        assert kb.block_task(conn, tid, reason="Stop", idempotency_key="request")
        before = kb.list_events(conn, tid)
        with pytest.raises(ValueError, match="conflicts"):
            kb.block_task(conn, tid, reason="different Stop", idempotency_key="request")
        with pytest.raises(ValueError, match="conflicts"):
            kb.unblock_task(conn, tid, idempotency_key="request")
        assert kb.list_events(conn, tid) == before
        assert kb.get_task(conn, tid).status == "blocked"


@pytest.mark.parametrize("operation", ["block", "unblock"])
def test_original_noop_receipt_cannot_execute_after_later_state_change(database, operation):
    opposite = "unblock" if operation == "block" else "block"
    with kbc.connect(database) as conn:
        tid = _create(conn, opposite)
        assert _call(conn, tid, operation, idempotency_key="no-op") is False
        assert _call(conn, tid, opposite)
        before = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        assert _call(conn, tid, operation, idempotency_key="no-op") is False
        assert kb.get_task(conn, tid) == before
        assert kb.list_events(conn, tid) == events


def test_concurrent_native_stop_and_anchored_resume_always_finish_stopped(database):
    with kbc.connect(database) as conn:
        tid = _create(conn, "unblock")
        kb.add_comment(conn, tid, "human", "Continue", idempotency_key="followup")
    gate = Barrier(2)

    def attempt(operation):
        with kbc.connect(database) as conn:
            gate.wait(timeout=10)
            if operation == "unblock":
                return kb.unblock_task(conn, tid, idempotency_key="resume", origin_comment_key="followup")
            return kb.block_task(conn, tid, reason="Stop", idempotency_key="stop")

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(attempt, operation) for operation in ("block", "unblock")]
        results = [job.result(timeout=20) for job in attempts]
        assert results in ([True, True], [False, False])
    with kbc.connect(database) as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        before = kb.list_events(conn, tid)
        assert kb.unblock_task(conn, tid, idempotency_key="resume", origin_comment_key="followup") == results[1]
        assert kb.list_events(conn, tid) == before
        assert kb.get_task(conn, tid).status == "blocked"


@pytest.mark.parametrize("typed", [False, True])
def test_stop_between_saved_instruction_and_first_resume_attempt(database, typed):
    with kbc.connect(database) as conn:
        tid = _create(conn, "unblock")
        if typed:
            assert kb.block_task(conn, tid, kind="needs_input")
        kb.add_comment(conn, tid, "human", "Continue with the saved checkpoint", idempotency_key="followup")
        assert kb.block_task(conn, tid, reason="Stop") is False
        assert kb.list_events(conn, tid)[-1].kind == "block_requested"
        assert kb.unblock_task(conn, tid, idempotency_key="resume", origin_comment_key="followup") is False
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.lifecycle_request_received(conn, tid, "unblock", "resume") is False
        assert kb.list_events(conn, tid)[-1].payload["refusal"] == "stale_native_event"
        # A distinct later instruction can resume; the refused old request
        # keeps its outcome even after another Stop.
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, reason="new Stop")
        before = kb.list_events(conn, tid)
        assert kb.unblock_task(conn, tid, idempotency_key="resume", origin_comment_key="followup") is False
        assert kb.list_events(conn, tid) == before
        assert kb.get_task(conn, tid).status == "blocked"


def test_saved_comment_survives_progress_but_not_later_native_ownership(database):
    with kbc.connect(database) as conn:
        tid = _create(conn, "unblock")
        kb.add_comment(conn, tid, "human", "Continue", idempotency_key="followup")
        kb.add_comment(conn, tid, "worker", "Checkpoint preserved")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "heartbeat", {"progress": "checkpointed"})
        assert kb.unblock_task(conn, tid, origin_comment_key="followup", idempotency_key="resume")
        assert kb.get_task(conn, tid).status == "ready"

        other = _create(conn, "unblock")
        kb.add_comment(conn, other, "human", "Continue", idempotency_key="followup")
        assert kb.assign_task(conn, other, "another-worker")
        assert kb.unblock_task(conn, other, origin_comment_key="followup", idempotency_key="resume") is False
        assert kb.get_task(conn, other).status == "blocked"


def test_initial_activation_uses_creation_event_fence(database):
    with kbc.connect(database) as conn:
        tid = _create(conn, "unblock")
        origin = kb.list_events(conn, tid)[-1].id
        assert kb.block_task(conn, tid, reason="Stop before activation") is False
        assert kb.unblock_task(conn, tid, idempotency_key="initial", expected_event_id=origin) is False
        assert kb.get_task(conn, tid).status == "blocked"

        fresh = _create(conn, "unblock")
        origin = kb.list_events(conn, fresh)[-1].id
        assert kb.unblock_task(conn, fresh, idempotency_key="initial", expected_event_id=origin)
        assert kb.block_task(conn, fresh, reason="later native Stop")
        before = kb.list_events(conn, fresh)
        assert kb.unblock_task(conn, fresh, idempotency_key="initial", expected_event_id=origin)
        assert kb.list_events(conn, fresh) == before
        assert kb.get_task(conn, fresh).status == "blocked"


@pytest.mark.parametrize("mutation", ["reassign", "edit", "import", "model", "reasoning"])
def test_saved_resume_refuses_native_handoff_or_task_definition_change(database, mutation):
    from hermes_cli.kanban_transfer import _relocate_imported_rows

    with kbc.connect(database) as conn:
        tid = _create(conn, "unblock")
        kb.add_comment(conn, tid, "human", "Continue", idempotency_key="followup")
        actions = {
            "reassign": lambda: kb.reassign_task(conn, tid, "another-worker"),
            "edit": lambda: kb.edit_task(conn, tid, body="A different task definition"),
            "import": lambda: _relocate_imported_rows(conn, "imported-board"),
            "model": lambda: kb.set_model_override(conn, tid, "different-model"),
            "reasoning": lambda: kb.set_reasoning_effort(conn, tid, "high"),
        }
        actions[mutation]()
        if mutation == "reassign":
            assert kb.get_task(conn, tid).assignee == "another-worker"
        assert kb.unblock_task(conn, tid, idempotency_key="resume", origin_comment_key="followup") is False
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.list_events(conn, tid)[-1].payload["refusal"] == "stale_native_event"


def test_late_stop_request_cannot_block_new_native_generation(database):
    with kbc.connect(database) as conn:
        tid = _create(conn, "block")
        origin = kb.list_events(conn, tid)[-1].id
        assert kb.block_task(conn, tid, reason="another Stop")
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, reason="old Stop", idempotency_key="stop",
                             expected_event_id=origin) is False
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.get_task(conn, tid).block_recurrences == 1


def test_missing_or_foreign_anchor_and_changed_fence_fail_closed(database):
    with kbc.connect(database) as conn:
        tid = _create(conn, "unblock")
        other = _create(conn, "unblock")
        foreign = kb.list_events(conn, other)[-1].id
        assert kb.unblock_task(conn, tid, expected_event_id=foreign, idempotency_key="resume") is False
        assert kb.unblock_task(conn, tid, origin_comment_key="missing", idempotency_key="missing") is False
        local = kb.list_events(conn, tid)[0].id
        with pytest.raises(ValueError, match="conflicts"):
            kb.unblock_task(conn, tid, expected_event_id=local, idempotency_key="resume")
        assert kb.lifecycle_request_received(conn, tid, "unblock", "never-seen") is None
        assert kb.get_task(conn, tid).status == "blocked"


def test_atomic_rollback_and_postcommit_callback_response_loss(database, monkeypatch):
    with kbc.connect(database) as conn:
        tid = _create(conn, "block")
        original = kb._append_event

        def fail_receipt(conn, task_id, kind, *args, **kwargs):
            if kind == "lifecycle_request":
                raise RuntimeError("simulated write failure")
            return original(conn, task_id, kind, *args, **kwargs)

        monkeypatch.setattr(kb, "_append_event", fail_receipt)
        with pytest.raises(RuntimeError, match="write failure"):
            kb.block_task(conn, tid, idempotency_key="stop")
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.lifecycle_request_received(conn, tid, "block", "stop") is None
        monkeypatch.setattr(kb, "_append_event", original)
        callbacks = []

        def response_lost(*args, **kwargs):
            callbacks.append(args[0])
            raise RuntimeError("simulated lost response after commit")

        monkeypatch.setattr(kb, "_fire_task_hook", response_lost)
        with pytest.raises(RuntimeError, match="lost response"):
            kb.block_task(conn, tid, idempotency_key="stop")
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.lifecycle_request_received(conn, tid, "block", "stop") is True
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, idempotency_key="stop")
        assert callbacks == ["kanban_task_blocked"]
        assert kb.get_task(conn, tid).status == "ready"


def test_request_receipts_survive_terminal_event_retention(database):
    with kbc.connect(database) as conn:
        tid = _create(conn, "unblock")
        comment = kb.add_comment(conn, tid, "human", "Continue", idempotency_key="followup")
        assert kb.unblock_task(conn, tid, idempotency_key="resume")
        assert kb.block_task(conn, tid, idempotency_key="stop")
        assert kb.archive_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=1 WHERE task_id=?", (tid,))
        assert kb.gc_events(conn, older_than_seconds=0) > 0
        assert kb.lifecycle_request_received(conn, tid, "unblock", "resume") is True
        assert kb.lifecycle_request_received(conn, tid, "block", "stop") is True
        assert kb.add_comment(conn, tid, "human", "Continue", idempotency_key="followup") == comment
        before = kb.list_events(conn, tid)
        assert kb.unblock_task(conn, tid, idempotency_key="resume")
        assert kb.get_task(conn, tid).status == "archived"
        assert kb.list_events(conn, tid) == before


def test_first_resume_after_retention_keeps_the_latest_native_control_fence(database):
    with kbc.connect(database) as conn:
        tid = _create(conn, "unblock")
        initial = kb.list_events(conn, tid)[-1].id
        kb.add_comment(conn, tid, "human", "Continue", idempotency_key="pending")
        comment_event = kb.list_events(conn, tid)[-1].id
        assert kb.block_task(conn, tid, reason="Stop before first resume") is False
        kb.add_comment(conn, tid, "worker", "Ordinary progress history")
        # GC only prunes terminal cards. Retiring the stopped card must not erase
        # the ordering fence while the old instruction receipt still survives.
        assert kb.archive_task(conn, tid)
        latest_control = kb.list_events(conn, tid)[-1].id
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=1 WHERE task_id=?", (tid,))
        assert kb.gc_events(conn, older_than_seconds=0) > 0
        assert [(event.id, event.kind) for event in kb.list_events(conn, tid)] == [
            (comment_event, "commented"), (latest_control, "archived"),
        ]
        assert kb.unblock_task(conn, tid, idempotency_key="pending-resume", origin_comment_key="pending") is False
        assert kb.list_events(conn, tid)[-1].payload["refusal"] == "stale_native_event"
        assert kb.unblock_task(conn, tid, idempotency_key="initial", expected_event_id=initial) is False
        assert kb.list_events(conn, tid)[-1].payload["refusal"] == "missing_native_event"
        assert kb.unblock_task(conn, tid, idempotency_key="event-anchor", expected_event_id=comment_event) is False
        assert kb.list_events(conn, tid)[-1].payload["refusal"] == "stale_native_event"
        assert kb.get_task(conn, tid).status == "archived"
