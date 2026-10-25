"""An admitted managed worker's real control frames cannot outlive its native runtime owner."""

import asyncio
from contextlib import contextmanager
from io import BytesIO
import json
import os
import threading
from types import SimpleNamespace

import pytest

from tests.gateway.test_native_cron_task_authority import _runtime


@contextmanager
def _worker_controls(monkeypatch, authority, ref, row):
    from agent import managed_worker
    from gateway.session_managed_worker import _prompt_frame

    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as incoming, os.fdopen(write_fd, "wb", buffering=0) as outgoing:
        monkeypatch.setattr(managed_worker.sys, "stdin", SimpleNamespace(buffer=incoming))
        owner_worker = SimpleNamespace(stop=threading.Event(), control=lambda frame: outgoing.write(managed_worker.encode_frame(frame)))

        def send(kind, **payload):
            frame = managed_worker.read_frame(BytesIO(managed_worker.encode_frame({"type": kind, **payload})))
            assert _prompt_frame(authority, ref, row, owner_worker, frame)

        controls = managed_worker.WorkerControls(SimpleNamespace(send=send), authority.sessions[ref.session_id].route)
        try:
            yield controls
        finally:
            outgoing.write(managed_worker.encode_frame({"type": "finish"}))
            controls.reader.join(timeout=10)
            assert not controls.reader.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["epoch", "retired"])
async def test_managed_native_bootstrap_and_task_check_reject_released_owner(tmp_path, monkeypatch, replacement):
    from agent.managed_worker import validate_bootstrap
    from gateway import session_managed_worker
    from gateway.session_contract import Principal, Submission
    from gateway.session_local import create_local_session
    from hermes_state_runtime import begin_runtime_epoch, get_session_admission
    from model_tools import handle_function_call
    from tools.approval_context import approval_task_context

    _runner, authority, registry = await _runtime(tmp_path, monkeypatch, paused=True)
    actor = Principal("human", authority.profile_id, frozenset({"session:create", "session:submit"}), "viewer")
    ref = create_local_session(authority, actor, dict(request_id="managed-human", source="cli", model="fixture",
        cwd=str(tmp_path), toolsets=[], ignore_user_config=True))
    await authority.submit(actor, Submission("managed-request", ref, {"text": "Write the authorized managed output"}, "queue"))
    row, _head = await authority._claim_next(ref, authority.sessions[ref.session_id])
    policy = session_managed_worker.managed_policy(authority, ref)
    scope = {"profile_id": authority.profile_id, "session_id": ref.session_id, "execution_id": "managed-epoch",
             "generation": row["generation"], "pid": os.getpid(), "birth": 0, "secret": "fixture-pipe", "epoch": authority.epoch}
    frame = validate_bootstrap(json.loads(json.dumps(session_managed_worker._bootstrap(authority, ref, row, policy, scope))))
    task = frame["approval_task_v1"]
    assert task["identity"] == actor.subject and task["admission"] == row["admission_id"]
    before, after = tmp_path / "current.txt", tmp_path / "released.txt"

    try:
        with _worker_controls(monkeypatch, authority, ref, row) as controls:
            assert await asyncio.to_thread(controls.task_current)

            def write(path):
                # Exactly the worker's consumer binding: all evidence comes from its native
                # bootstrap, and every live check traverses WorkerControls and owner frames.
                with approval_task_context(task["text"], identity=task["identity"], profile=task["profile"],
                    session=task["session"], task_id=task["admission"], run_id=str(task["generation"]),
                    is_current=controls.task_current):
                    return json.loads(handle_function_call("write_file", {"path": str(path), "content": "managed"},
                                                           task_id="managed-epoch"))

            current = await asyncio.to_thread(write, before)
            assert before.read_text() == "managed", current
            if replacement == "epoch":
                successor = begin_runtime_epoch(authority.db, instance_id="successor")
                assert successor != authority.epoch
            else:
                from gateway.run_runtime import _retire_profile_authority
                assert await _retire_profile_authority(authority, timeout=1.0)
                assert authority.retired
            # A current-epoch check must not confuse an unchanged historical claim with
            # present ownership; neither advancing the epoch nor retirement rewrites it.
            committed = get_session_admission(authority.db, admission_id=row["admission_id"])
            assert committed["status"] == "started"
            assert committed["owner_epoch"] == row["owner_epoch"] == authority.epoch
            stale = await asyncio.to_thread(write, after)
            assert stale.get("code") == "task_authority_revoked", stale
            assert not after.exists()
            assert not await asyncio.to_thread(controls.task_current)
            late = session_managed_worker._bootstrap(authority, ref, row, policy, scope)
            assert "approval_task_v1" not in late
    finally:
        await asyncio.to_thread(registry.kill_all, source="test-cleanup")
