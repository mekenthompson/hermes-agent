"""A card attempt cut off by a restart of its whole instance settles interrupted, so its respawn runs.

HF-617 (2026-11-02): ``docker compose restart`` killed Klanker's managed interpreter before the
gateway could Stop it. The owner fenced the Stop, recorded the admission ``unknown`` and was killed
in turn. The new instance could not reconcile processes it never ran, so the respawn was refused
(``kanban_previous_execution_unknown``), both attempts were charged to the card, it gave up, and a
takeover's cleanup check still read the old attempt as unknown. Every process of the old attempt
was in the PID namespace that ended with the old PID 1: that is the proof, and nothing less.
"""
import asyncio
import json
import subprocess
import sys
from contextlib import closing

import psutil
import pytest

from tests.gateway.test_kanban_finite_admission import _CONFIG, kanban_owner  # kanban_owner: the fixture, by name



@pytest.fixture
def card_owner(request):
    """The shared real owner authority with a claimed card (``kanban_owner``), built for cards."""
    return request.getfixturevalue('kanban_owner')(_CONFIG)


OLD_INSTANCE = '00000000-0000-4000-8000-000000000000:1'  # an instantiation that is not this one


def _absent_pid(base):
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


def _admission(db, sid):
    from hermes_state_runtime import list_session_admissions
    [row] = [row for row in list_session_admissions(db, session_id=sid, pending_only=False)
             if row['request_id'].startswith('kanban:')]
    return row['status'], row['outcome']


def _checkpoint_ids():
    from tools.process_registry import _checkpoint_path
    path = _checkpoint_path()
    return {entry['session_id'] for entry in json.loads(path.read_text())} if path.exists() else set()


class _Restart:
    """Run 1 is executing in the old instance when the whole instance restarts."""

    def __init__(self, owner, monkeypatch, tmp_path):
        self.owner, self.monkeypatch, self.tmp_path = owner, monkeypatch, tmp_path
        self.children = []

    async def admit_and_lose(self, *, worker='dead', owner_alive=False, record='dead'):
        from gateway.session_ancillary import record_process_stop
        from gateway.session_contract import SessionRef
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect
        from hermes_cli.kanban_db_dispatch import _process_fingerprint
        from hermes_state_runtime import claim_session_input
        from tools.process_registry import _checkpoint_path, process_registry
        owner, authority = self.owner, self.owner.authority
        self.sid = (await run_kanban(owner.connection, owner.params))['session_id']
        self.ref = SessionRef(authority.profile_id, self.sid)
        params = owner.params
        if worker == 'dead':
            worker_pid, fingerprint = _absent_pid(3_810_000), f'{OLD_INSTANCE}|4162240'
        else:  # an interpreter still running in THIS instance
            worker_pid = self.child().pid
            fingerprint = _process_fingerprint(worker_pid)
        with closing(connect(board='owned')) as conn:
            if not owner_alive:  # run_task recorded this test process: the old gateway died
                marker = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND run_id=? "
                                      "AND kind='owner_admitted'", (params['task_id'], params['run_id'])).fetchone()
                payload = {**json.loads(marker[1]), 'pid': _absent_pid(3_820_000), 'birth': 1.0}
                conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), marker[0]))
            for kind in ('spawned', 'worker_bound'):
                kb._append_event(conn, params['task_id'], kind, {'pid': worker_pid, 'started_at': fingerprint,
                                 'claim_lock': params['claim_lock']}, run_id=params['run_id'])
            kb._append_event(conn, params['task_id'], 'worker_lost', {  # the owner saw it die first
                'reason': 'EOFError', 'claim_lock': params['claim_lock'], 'run_id': params['run_id'],
                'pid': worker_pid, 'bound': True}, run_id=params['run_id'])
            conn.execute('UPDATE tasks SET worker_pid=?, worker_started_at=?, started_at=started_at-3600 WHERE id=?',
                         (worker_pid, fingerprint, params['task_id']))  # past the sweep's launch grace
            conn.commit()
        # A background job the turn started: the restart killed it before it wrote its exit receipt.
        job_pid = _absent_pid(3_830_000)
        host_start = 4162250
        if record == 'live':  # the same pid AND birth is alive here: never discharged
            job_pid = self.child().pid
            host_start = process_registry._safe_host_start_time(job_pid)
        route = authority.sessions[self.sid].route
        self.record = 'proc_restartjob1'
        _checkpoint_path().write_text(json.dumps([{
            'session_id': self.record, 'command': 'sleep 600', 'pid': job_pid, 'pid_scope': 'host',
            'host_start_time': host_start, 'native_job_scope': True, 'cwd': str(self.tmp_path),
            'started_at': 1.0, 'task_id': self.sid, 'owner_task_id': self.sid, 'session_key': route}]))
        # The interpreter dies first: the owner fences its Stop, then dies with the turn ``started``.
        claimed = claim_session_input(authority.db, epoch=authority.epoch, session_id=self.sid)
        record_process_stop(authority, self.ref, claimed['generation'])
        return self.restart()

    def child(self):
        command = [sys.executable, '-c', 'import threading; threading.Event().wait(120)']
        child = subprocess.Popen(command)
        self.children.append(child)
        return child

    def restart(self):
        """A new owner epoch boots on the same stores with a fresh process registry."""
        from gateway.config import Platform
        from gateway.session_authority import SessionAuthority
        from gateway.session_controls import AuthorityConnection
        from gateway.session_unattended_unknown import settle_recovered_unattended
        from hermes_state_runtime import begin_runtime_epoch, recover_session_inputs
        from tools import process_registry as module
        old = self.owner.authority
        db = old.db
        epoch = begin_runtime_epoch(db, instance_id='owner-2')
        assert recover_session_inputs(db, epoch=epoch) == 1
        settle_recovered_unattended(db, epoch)
        assert _admission(db, self.sid) == ('unknown', None), 'boot recovery skips a fenced row'
        old.runner.adapters.pop(Platform.LOCAL, None)
        self.monkeypatch.setattr(module, 'process_registry', module.ProcessRegistry())
        authority = SessionAuthority(old.runner, profile_id=old.profile_id, instance_id='owner-2', db=db, epoch=epoch)
        self.monkeypatch.setattr(authority, '_schedule', lambda ref: None)
        old.runner.session_authority = authority
        self.authority = authority
        self.connection = AuthorityConnection(authority, object(), {
            'user_id': 'owner', 'provider': 'local', 'profile_id': authority.profile_id,
            'instance_id': authority.instance_id, 'native_bootstrap': True,
            'capabilities': ['session:create', 'session:read', 'session:submit', 'session:control']})
        return authority

    def respawn(self):
        """The dispatcher's crash sweep books run 1 and claims run 2."""
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_dispatch as kbd
        from hermes_cli.kanban_db_connect import connect
        tid = self.owner.params['task_id']
        with closing(connect(board='owned')) as conn:
            assert tid in kbd.detect_crashed_workers(conn)
            task = kb.claim_task(conn, tid)
        assert task is not None and task.current_run_id != self.owner.params['run_id']
        return dict(self.owner.params, run_id=task.current_run_id, claim_lock=task.claim_lock)

    def close(self):
        for child in self.children:
            child.kill()
            child.wait()


async def run_kanban(connection, params):
    from gateway.session_kanban import run_task
    return await run_task(connection, params)


def _task(tid):
    from hermes_cli.kanban_db_connect import connect
    with closing(connect(board='owned')) as conn:
        row = conn.execute('SELECT status, consecutive_failures FROM tasks WHERE id=?', (tid,)).fetchone()
        kinds = [r[0] for r in conn.execute('SELECT kind FROM task_events WHERE task_id=? ORDER BY id', (tid,))]
    return tuple(row), kinds


@pytest.mark.parametrize('at_startup', [True, False], ids=['settled-at-startup', 'settled-by-respawn'])
def test_restart_mid_run_settles_the_lost_attempt_and_its_respawn_completes(card_owner, monkeypatch, tmp_path, at_startup):
    from gateway.session_ancillary import process_stop_pending
    from gateway.session_kanban_stop import settle_replaced_card_attempts
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    owner = card_owner
    restart = _Restart(owner, monkeypatch, tmp_path)

    async def scenario():
        try:
            authority = await restart.admit_and_lose()
            if at_startup:
                await settle_replaced_card_attempts(authority)
                assert _admission(authority.db, restart.sid) == ('terminal', 'interrupted')
                assert process_stop_pending(authority.db, restart.sid) is None
                assert restart.record not in _checkpoint_ids()
            params = restart.respawn()
            status, kinds = _task(params['task_id'])
            assert status == ('running', 0), 'a restart of the whole instance is not the card failing'
            # The respawn is admitted by the previous-execution fence.
            accepted = await run_kanban(restart.connection, params)
            assert accepted['session_id'] != restart.sid and accepted['receipt']['status'] == 'queued'
            assert _admission(authority.db, restart.sid) == ('terminal', 'interrupted')
            assert process_stop_pending(authority.db, restart.sid) is None
            assert restart.record not in _checkpoint_ids()
            with closing(connect(board='owned')) as conn:
                assert kb.complete_task(conn, params['task_id'], summary='done', expected_run_id=params['run_id'])
            status, kinds = _task(params['task_id'])
            assert status[0] == 'done' and 'gave_up' not in kinds
            await restart.connection.close()
        finally:
            restart.close()
            await owner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('shape', [
    dict(worker='live'),                  # its interpreter is still running in this instance
    dict(owner_alive=True),               # the gateway that admitted it is still alive
    dict(record='live'),                  # a process it started is alive here with its birth
], ids=['live-worker', 'live-owner', 'live-process'])
def test_an_attempt_that_might_still_run_keeps_its_fence(card_owner, monkeypatch, tmp_path, shape):
    from gateway.session_ancillary import process_stop_pending
    from gateway.session_kanban_stop import settle_replaced_card_attempts
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_state_runtime import RuntimeStoreError
    owner = card_owner
    restart = _Restart(owner, monkeypatch, tmp_path)

    async def scenario():
        try:
            authority = await restart.admit_and_lose(**shape)
            await settle_replaced_card_attempts(authority)
            assert _admission(authority.db, restart.sid) == ('unknown', None)
            assert process_stop_pending(authority.db, restart.sid) is not None
            assert restart.record in _checkpoint_ids()
            tid = owner.params['task_id']
            with closing(connect(board='owned')) as conn:  # an operator re-queues the card regardless
                conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=1 WHERE id=?",
                             (owner.params['run_id'],))
                conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
                             "current_run_id=NULL, worker_pid=NULL WHERE id=?", (tid,))
                conn.commit()
                task = kb.claim_task(conn, tid)
            params = dict(owner.params, run_id=task.current_run_id, claim_lock=task.claim_lock)
            with pytest.raises(RuntimeStoreError, match='kanban_previous_execution_unknown'):
                await run_kanban(restart.connection, params)
            assert _admission(authority.db, restart.sid) == ('unknown', None)
            assert process_stop_pending(authority.db, restart.sid) is not None
            await restart.connection.close()
        finally:
            restart.close()
            await owner.close()

    asyncio.run(scenario())
