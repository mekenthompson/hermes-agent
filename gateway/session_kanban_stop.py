"""Retire earlier native attempts before another attempt may execute the same task."""
import asyncio
from contextlib import closing
from functools import partial
import json
from pathlib import Path
import sqlite3

from hermes_state_runtime import RuntimeStoreError


async def worker_control_frame(authority, ref, row, policy, worker, scope, frame):
    if frame == {'type': 'ready', 'pid': scope['pid']}:
        return True
    if frame == {'type': 'kanban_stop'} and policy.kanban_json is not None:
        from gateway.session_ancillary import record_process_stop
        from gateway.session_runtime_workers import tracked_write
        await tracked_write(authority, partial(record_process_stop, authority, ref, row['generation']))
        worker.control({'type': 'stop', 'processes': True})
        return True
    return False


def attempt_aborted(context):
    with closing(sqlite3.connect(Path(context['db']).as_uri() + '?mode=ro', uri=True)) as conn:
        row = conn.execute('SELECT status FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    return row is None or row[0] not in {'done', 'review', 'scheduled'}


async def retain_failed_stop(authority, ref, row, policy):
    if policy.kanban_json is not None and await asyncio.to_thread(attempt_aborted, json.loads(policy.kanban_json)):
        from gateway.session_ancillary import record_process_stop
        from gateway.session_runtime_workers import tracked_write
        await tracked_write(authority, partial(record_process_stop, authority, ref, row['generation']))


def require_current_claim(conn, context):
    row = conn.execute(
        'SELECT t.status,t.current_run_id,t.claim_lock,r.status,r.claim_lock '
        'FROM tasks t JOIN task_runs r ON r.task_id=t.id WHERE t.id=? AND r.id=?',
        (context['task_id'], context['run_id'])).fetchone()
    if row is None or tuple(row) != ('running', context['run_id'], context['claim_lock'], 'running', context['claim_lock']):
        raise RuntimeStoreError('stale_kanban_claim')


def previous_attempts(context):
    with closing(sqlite3.connect(Path(context['db']).as_uri() + '?mode=ro', uri=True)) as conn:
        require_current_claim(conn, context)
        rows = conn.execute(
            "SELECT e.run_id,e.payload FROM task_events e LEFT JOIN task_runs r ON r.id=e.run_id "
            "WHERE e.task_id=? AND e.kind='owner_admitted' AND e.run_id<? "
            "AND (r.status IS NULL OR r.status NOT IN ('done','review','scheduled') OR EXISTS ("
            "SELECT 1 FROM task_events stop WHERE stop.task_id=e.task_id AND stop.id>e.id "
            "AND stop.kind IN ('blocked','block_loop_detected'))) ORDER BY e.id",
            (context['task_id'], context['run_id'])).fetchall()
    return [{**json.loads(row[1]), 'run_id': row[0]} for row in rows]


def check_previous(authority, context, binding):
    from gateway.session_policy import restore_policy
    from hermes_state_local import local_receipt
    if Path(binding['db']).resolve() != Path(authority.db.db_path).resolve():
        raise RuntimeStoreError('kanban_previous_owner_unavailable')
    saved = local_receipt(authority.db, binding['session_id'])
    policy = restore_policy(saved['policy'])
    old = json.loads(policy.kanban_json or '{}')
    if (policy.source != 'kanban' or old.get('run_id') != binding['run_id']
            or old.get('task_id') != context['task_id'] or old.get('board') != context['board']
            or Path(old.get('db', '')).resolve() != Path(context['db']).resolve()):
        raise RuntimeStoreError('invalid_kanban_claim')


def previous_workers_gone(context, binding):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _poll_worker_exit, _process_fingerprint, _worker_alive
    with closing(sqlite3.connect(Path(context['db']).as_uri() + '?mode=ro', uri=True)) as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
            "AND kind IN ('spawned','worker_registered','worker_bound')", (context['task_id'], binding['run_id'])).fetchall()
    workers = [json.loads(row[0]) for row in rows]
    for worker in workers:
        pid = worker.get('pid')
        if type(pid) is not int or pid <= 0:
            return False
        if kb._pid_alive(pid):
            if _process_fingerprint(pid) is None or not _poll_worker_exit(pid, worker.get('started_at')):
                return False
            if kb._pid_alive(pid) and (_process_fingerprint(pid) is None or _worker_alive(pid, worker.get('started_at'))):
                return False
    return True


def _drop_replaced_records(authority, ref):
    """Retire this session's checkpoint records that a replaced instantiation left behind.

    Only after ``attempt_instance_replaced``. A killed job never writes the exit receipt the
    native Stop needs, so its record would fence every later attempt. Each record must be an
    ordinary host process (a sandbox, WSL launcher or systemd unit can outlive a PID namespace)
    whose pid is not alive here with its recorded birth; a process this registry tracks stays
    the native Stop's to reconcile. False when anything might still be running.
    """
    from gateway.session_ancillary import _owned_locked, _ownership
    from tools.process_registry import _checkpoint_path, process_registry
    keys, owners, parents = _ownership(authority, ref, None, ())
    with process_registry._lock:
        tracked = {*process_registry._running, *process_registry._finished}
        if any(not process.exited for process in _owned_locked(process_registry, keys, owners, parents)):
            return False
    path = _checkpoint_path()
    try:
        entries = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else []
    except (OSError, ValueError):
        return False
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        return False
    stale = set()
    for entry in entries:
        if entry.get('session_key') not in keys or entry.get('session_id') in tracked or not (
                entry['parent_session_id'] in parents if entry.get('parent_session_id')
                else (entry.get('owner_task_id') or entry.get('task_id')) in owners):
            continue
        pid = entry.get('pid')
        if (entry.get('pid_scope', 'host') != 'host' or entry.get('wsl_chain') or entry.get('systemd_unit')
                or type(pid) is not int or pid <= 0
                or process_registry._detached_host_fate(pid, entry.get('host_start_time')) == 'running'):
            return False
        stale.add(entry.get('session_id'))
    process_registry._write_checkpoint(exclude_ids=stale)  # also leaves a readable checkpoint behind
    return True


def replaced_attempt(context, run_id, session_id):
    from hermes_cli.kanban_owner_recovery import attempt_instance_replaced
    try:
        with closing(sqlite3.connect(Path(context['db']).as_uri() + '?mode=ro', uri=True)) as conn:
            return attempt_instance_replaced(conn, context['task_id'], run_id, session_id)
    except sqlite3.Error:
        return False  # an unreadable board is no proof; the native fence decides


async def discharge_replaced_instance(authority, ref, context, run_id):
    """Prove an attempt died with an earlier instantiation of this host; retire what it left.

    A restart that takes the container (or machine) down kills the attempt's interpreter before
    its owner can Stop it: the owner fences the Stop, records the admission ``unknown`` and dies,
    and the successor cannot reconcile processes it never ran. Only rows from an earlier
    authority epoch qualify: a Stop this owner recorded, or any other pending row, keeps the
    native fence. The caller still runs the native Stop retry, which clears the fence.
    """
    from gateway.session_ancillary import process_stop_pending
    from gateway.session_authorities import owner_scope
    from hermes_state_runtime import list_session_admissions
    token = process_stop_pending(authority.db, ref.session_id)
    if token is not None and token.partition(':')[0] == str(authority.epoch):
        return False
    rows = await asyncio.to_thread(list_session_admissions, authority.db, session_id=ref.session_id)
    if any(row['status'] != 'unknown' or row['owner_epoch'] >= authority.epoch for row in rows):
        return False
    if not await asyncio.to_thread(replaced_attempt, context, run_id, ref.session_id):
        return False
    with owner_scope(authority):
        return await asyncio.to_thread(_drop_replaced_records, authority, ref)


async def settle_replaced_card_attempts(authority):
    """Startup: settle each card attempt an earlier instantiation left ``unknown`` behind a Stop.

    Boot recovery skips a fenced row, and until it settles the attempt reads as unknown to a
    takeover's cleanup check. Runs before the dispatcher, so the crash sweep sees the attempt
    settled ``interrupted``. Unproved attempts keep their fence for the respawn to retry.
    """
    import logging
    from gateway.session_ancillary import process_stop_pending, retry_process_stop
    from gateway.session_contract import SessionRef
    from gateway.session_local_recovery import restore_local_session
    from gateway.session_policy import restore_policy
    from gateway.session_unattended_unknown import is_unattended, settle_unattended_unknown
    from hermes_state_local import local_receipt
    from hermes_state_runtime import _row
    logger = logging.getLogger(__name__)
    try:
        rows = [_row(row) for row in authority.db._read_all(
            "SELECT * FROM session_admissions WHERE status='unknown' ORDER BY seq")]
    except (RuntimeStoreError, sqlite3.Error) as exc:
        logger.warning('Card attempts from an earlier instance left for their respawns: %s', type(exc).__name__)
        return
    for row in rows:
        sid, card = row['target_session_id'], (row.get('payload') or {}).get('kanban_card_v1')
        if (not card or row['owner_epoch'] >= authority.epoch or not is_unattended(row)
                or process_stop_pending(authority.db, sid) is None):
            continue  # an unfenced card row already settled in boot recovery
        try:
            policy = restore_policy(local_receipt(authority.db, sid)['policy'])
            context = json.loads(policy.kanban_json or '{}')
            if policy.source != 'kanban' or any(context.get(key) != card.get(key) for key in ('board', 'task_id', 'run_id')):
                continue
            if sid not in authority.sessions:
                restore_local_session(authority, sid)
            ref = SessionRef(authority.profile_id, sid)
            async with authority.sessions[sid].claim_gate:
                if (sid in authority.executing
                        or not await discharge_replaced_instance(authority, ref, context, card['run_id'])
                        or not await retry_process_stop(authority, ref)):
                    continue
            if await settle_unattended_unknown(authority, ref):
                logger.info('Card attempt %s (%s run %s) ended with an earlier instance: settled interrupted',
                            sid, card['task_id'], card['run_id'])
        except (RuntimeStoreError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            logger.warning('Card attempt %s left for its respawn: %s', sid, getattr(exc, 'reason', type(exc).__name__))


async def retire_previous_attempts(connection, context):
    """An authorized new claim can stop its aborted predecessor, never repeat its effects.

    The existing board events identify the native owner session. Its ordinary Stop,
    execution task and process checkpoint establish quiescence, including after reload;
    another claim or a dead interpreter by itself cannot establish that.
    """
    from gateway.session_ancillary import process_stop_pending, record_process_stop, retry_process_stop
    from gateway.session_contract import SessionRef
    from gateway.session_managed_worker import STOP_ACK_SECONDS, ManagedExecutionUnknown
    from gateway.session_runtime_workers import tracked_write

    authority = connection.authority
    prior = await asyncio.to_thread(previous_attempts, context)
    seen = set()
    for binding in prior:
        sid = binding['session_id']
        if sid in seen:
            continue
        seen.add(sid)
        await asyncio.to_thread(check_previous, authority, context, binding)
        ref = SessionRef(authority.profile_id, sid)
        await connection.resume(ref, {})
        handle = await authority.resolve(connection.actor, ref)
        if sid in authority.executing:
            try:
                await connection.interrupt(ref, {'execution_generation': handle.execution_generation})
            except RuntimeStoreError as exc:
                if exc.reason != 'managed_worker_lost':
                    raise
        task = authority.sessions[sid].task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), STOP_ACK_SECONDS + 10)
            except TimeoutError as exc:
                raise RuntimeStoreError('kanban_previous_execution_unknown') from exc
            except ManagedExecutionUnknown:
                pass  # retain its unknown result; physical cleanup below does not replay that turn
        async with authority.sessions[sid].claim_gate:
            if sid in authority.executing:
                raise RuntimeStoreError('kanban_previous_execution_unknown')
            gone = await asyncio.to_thread(previous_workers_gone, context, binding)
            # Before this owner records its own Stop: only an earlier epoch's fence can qualify.
            replaced = gone and await discharge_replaced_instance(authority, ref, context, binding['run_id'])
            handle = await authority.resolve(connection.actor, ref)
            await tracked_write(authority, partial(record_process_stop, authority, ref, handle.execution_generation))
            if (not gone
                    or not await retry_process_stop(authority, ref)
                    or process_stop_pending(authority.db, sid) is not None):
                raise RuntimeStoreError('kanban_previous_execution_unknown')
        if replaced:
            # Its fence is gone, so its ``unknown`` card row settles interrupted (GW-10), as boot
            # recovery would have: a takeover's cleanup check then reads the attempt as settled.
            from gateway.session_unattended_unknown import settle_unattended_unknown
            await settle_unattended_unknown(authority, ref)
