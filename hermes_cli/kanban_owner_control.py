"""Supervisor Stop on an existing native owner, at the original run's generation."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3

import psutil
from websockets.exceptions import WebSocketException

logger = logging.getLogger(__name__)
STOP_TIMEOUT_SECONDS = 25
_STOP_REFUSALS = frozenset({'stale_generation', 'unknown_execution', 'permission_denied',
                            'not_found', 'profile_mismatch', 'stale_epoch'})
_ABORTED_OWNER_OUTCOMES = frozenset({'reclaimed', 'stale', 'timed_out', 'crashed', 'gave_up', 'spawn_failed'})


def _stop_intent(conn, task_id, run_id):
    from hermes_cli import kanban_db as kb
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                       "AND kind='native_owner_stop' ORDER BY id DESC LIMIT 1", (task_id, run_id)).fetchone()
    try:
        payload = json.loads(row['payload']) if row else {}
    except (ValueError, TypeError):
        payload = {}
    if isinstance(payload, dict) and payload.get('status') == 'pending':
        return
    kb._append_event(conn, task_id, 'native_owner_stop',
                     {'status': 'pending', 'reason': 'supervisor_stop'}, run_id=run_id)


def _missing_stop_scope(conn, task_id):
    from hermes_cli import kanban_db as kb
    kb._append_event(conn, task_id, 'native_owner_stop',
        {'status': 'unknown', 'reason': 'missing_original_stop_scope'})
    logger.warning('kanban: native Stop unknown for task %s; original run scope is missing or ambiguous', task_id)


def capture_supervisor_stop(conn, task_id, *, before_event_id=None, run_ids=None):
    """Freeze known original run coordinates and deny effects inside the existing writer.

    Request replays supply their saved IDs and event boundary. No lookup of the
    task's current run or current owner generation may widen an old Stop.
    """
    from hermes_cli.kanban_owner_recovery import owner_stop_confirmed
    boundary = '' if before_event_id is None else ' AND e.id<?'
    args = (task_id,) if before_event_id is None else (task_id, before_event_id)
    markers = conn.execute(
        "SELECT e.run_id,e.payload,r.id FROM task_events e LEFT JOIN task_runs r "
        "ON r.task_id=e.task_id AND r.id=e.run_id WHERE e.task_id=? AND e.kind='owner_admitted'" +
        boundary + ' ORDER BY e.id', args).fetchall()
    native = {row['run_id']: row for row in markers if row['id'] is not None}
    if run_ids is None:
        targets = set(native)
        if before_event_id is None:
            targets.update(row['id'] for row in conn.execute(
                'SELECT id FROM task_runs WHERE task_id=? AND worker_pid IS NOT NULL', (task_id,)))
        if any(row['id'] is None for row in markers):
            _missing_stop_scope(conn, task_id)
    else:
        if not isinstance(run_ids, list) or any(type(value) is not int or value < 1 for value in run_ids):
            _missing_stop_scope(conn, task_id)
            return []
        targets = set(run_ids)
        for run_id in targets:
            known = conn.execute('SELECT 1 FROM task_runs r WHERE r.task_id=? AND r.id=? AND EXISTS '
                '(SELECT 1 FROM task_events e WHERE e.task_id=r.task_id AND e.run_id=r.id AND e.id<?)',
                (task_id, run_id, before_event_id)).fetchone()
            if known is None:
                _missing_stop_scope(conn, task_id)
                return []
    for run_id in sorted(targets & native.keys()):
        try:
            if owner_stop_confirmed(conn, task_id, run_id, json.loads(native[run_id]['payload'])):
                continue
        except (ValueError, TypeError, KeyError):
            # Unreadable bindings are denied here and reported unknown by exact-run cleanup.
            logger.warning('kanban: native Stop binding unreadable for task %s run %s', task_id, run_id)
        _stop_intent(conn, task_id, run_id)
    return sorted(targets)


def replay_supervisor_stop(conn, task_id, saved):
    """Retry physical cleanup at a receipt's immutable original scope, with its bool unchanged."""
    if saved.get('refusal'):
        return []
    if 'stop_run_ids' in saved:
        return capture_supervisor_stop(conn, task_id, before_event_id=saved['_event_id'],
                                       run_ids=saved['stop_run_ids'])
    # Pre-scope receipts retain their original event boundary. Only native scopes
    # already admitted before that request can be retried, including after GC.
    return capture_supervisor_stop(conn, task_id, before_event_id=saved['_event_id'])


def _admission(binding):
    """Read the exact producer request without opening a writable SessionDB."""
    if not Path(binding['db']).is_absolute():
        raise ValueError('native admission database is not absolute')
    path = Path(binding['db']).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as owner:
        owner.row_factory = sqlite3.Row
        owner.execute('BEGIN')
        rows = owner.execute(
            'SELECT admission_id,target_session_id,request_id,generation,owner_epoch,status,outcome '
            'FROM session_admissions WHERE target_session_id=? AND request_id=?',
            (binding['session_id'], binding['request_id'])).fetchall()
        epoch = owner.execute('SELECT epoch,instance_id FROM runtime_epoch WHERE singleton=1').fetchone()
    if len(rows) != 1 or epoch is None:
        raise ValueError('original native admission is missing or ambiguous')
    row = dict(rows[0])
    # Unknown admits cancellation of this same scope, never new execution.
    if (row['status'] not in {'started', 'terminal', 'unknown'} or type(row['generation']) is not int
            or row['generation'] < 1
            or row['owner_epoch'] != epoch['epoch']):
        raise ValueError('original native admission is not a current execution generation')
    return dict(row, home=path.parent, epoch=epoch['epoch'], instance_id=epoch['instance_id'])


def _owner_alive(binding):
    from hermes_cli.process_identity import process_birth
    return (type(binding['pid']) is int and binding['birth'] is not None
            and process_birth(psutil.Process(binding['pid'])) == binding['birth'])


async def _interrupt(binding, admitted):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.gateway_client import connect_gateway
    from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint

    token = set_hermes_home_override(admitted['home'])
    try:
        observed = await asyncio.to_thread(discover_gateway_endpoint, admitted['home'], timeout=2)
        endpoint = observed.endpoint
        if (not attachable(observed) or endpoint.authority_epoch != admitted['epoch']
                or endpoint.instance_id != admitted['instance_id']
                or endpoint.profile_id != str(admitted['home'])
                or not await asyncio.to_thread(_owner_alive, binding)):
            raise ValueError('original native owner is unavailable or replaced')
        # Explicit endpoint is essential: the default connector can start a new owner.
        async with connect_gateway(endpoint=endpoint) as client:
            result = await client.rpc('session.interrupt', _timeout=STOP_TIMEOUT_SECONDS,
                session_id=binding['session_id'], execution_generation=admitted['generation'])
        ref = result.get('ref') if isinstance(result, dict) else None
        if (not isinstance(ref, dict) or ref.get('session_id') != binding['session_id']
                or result.get('execution_generation') != admitted['generation']):
            raise ValueError('native Stop returned a different execution generation')
        if result.get('execution_state') not in {'idle', 'running', 'waiting', 'terminal'}:
            raise ValueError('native Stop execution state is unknown')
        return result['execution_state']
    finally:
        reset_hermes_home_override(token)


def interrupt_owner_run(conn, run_id, *, allow_running=False):
    """True: joined effect scope; False: unknown; None: legacy with no owner marker.

    Called only after the supervisor block commits. A missing reply or stale owner is
    uncertainty on this exact attempt; never refresh to the session's current generation.
    ``native_owner_stop`` is an audit event, not an alternative execution authority.
    """
    from hermes_cli import kanban_db as kb

    run = conn.execute('SELECT task_id FROM task_runs WHERE id=?' +
                       ('' if allow_running else ' AND ended_at IS NOT NULL'),
                       (run_id,)).fetchone()
    if run is None:
        return None
    markers = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                          "AND kind='owner_admitted' ORDER BY id DESC",
                          (run['task_id'], run_id)).fetchall()
    if not markers:
        return None
    payload = {'status': 'unknown', 'reason': 'invalid_owner_marker'}
    try:
        if conn.in_transaction:
            raise ValueError('native Stop requires a committed supervisor block')
        if len({row['payload'] for row in markers}) != 1:
            raise ValueError('native owner marker is ambiguous')
        binding = json.loads(markers[0]['payload'])
        if not isinstance(binding, dict) or not isinstance(binding.get('request_id'), str):
            raise ValueError('native owner marker is unreadable')
        request = json.loads(binding['request_id'].removeprefix('kanban:'))
        if (not binding['request_id'].startswith('kanban:') or not isinstance(request, list)
                or len(request) != 3 or request[1:] != [run['task_id'], run_id]
                or type(request[2]) is not int
                or not isinstance(binding['session_id'], str) or not binding['session_id']):
            raise ValueError('native owner marker does not identify the original task attempt')
        payload.update(session_id=binding['session_id'], request_id=binding['request_id'])
        from hermes_cli.kanban_owner_recovery import owner_stop_confirmed
        if owner_stop_confirmed(conn, run['task_id'], run_id, binding):
            return True  # an old Stop replay never controls a later generation
        with kb.write_txn(conn):
            _stop_intent(conn, run['task_id'], run_id)
        payload['reason'] = 'missing_or_stale_admission'
        admitted = _admission(binding)
        payload.update(admission_id=admitted['admission_id'], generation=admitted['generation'])
        payload['reason'] = 'owner_unavailable_or_replaced'
        if not _owner_alive(binding):
            raise ValueError('original native owner process is unavailable or replaced')
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            payload['reason'] = 'owner_control_unconfirmed'
            payload['execution_state'] = asyncio.run(asyncio.wait_for(
                _interrupt(binding, admitted), timeout=STOP_TIMEOUT_SECONDS + 8))
        else:
            raise ValueError('native Stop must run off the owner event loop')
        payload['status'] = 'confirmed'
        # Compute may still be stopping under its existing supervised budget.
        # Confirmation speaks only for joined resources and revoked effect authority.
        payload['confirmation'] = 'effect_scope_cleaned'
        payload.pop('reason')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error, psutil.Error,
            WebSocketException) as exc:
        # Do not copy server/client exception text into the durable board: it can contain
        # transport details. The bounded reason and diagnostic identify uncertainty.
        from hermes_cli.gateway_client import GatewayRPCError
        if isinstance(exc, GatewayRPCError) and str(exc) in _STOP_REFUSALS:
            payload['reason'] = str(exc)
        logger.warning('kanban: native Stop unknown for task %s run %s (%s); '
                       'reconcile the original owner/process effects before retrying',
                       run['task_id'], run_id, payload['reason'])
    with kb.write_txn(conn):
        kb._append_event(conn, run['task_id'], 'native_owner_stop', payload, run_id=run_id)
    return payload['status'] == 'confirmed'


def owner_termination_ready(conn, run_id):
    """Cancel explicit/aborted scopes before killing their submitter; normal release is distinct."""
    return (run_id is None or not owner_scope_stop_required(conn, run_id)
            or interrupt_owner_run(conn, run_id, allow_running=True) is not False)


def owner_scope_stop_required(conn, run_id):
    """A retained submitter's exit is not a request to cancel its released services."""
    row = conn.execute('SELECT task_id,outcome,ended_at FROM task_runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        return False
    task_id = row['task_id']
    if row['ended_at'] is None or row['outcome'] in _ABORTED_OWNER_OUTCOMES:
        return True
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
                    "AND kind='native_owner_stop' LIMIT 1", (task_id, run_id)).fetchone():
        return True
    # Older supervisor Stops may have only the task-scoped request event. Its
    # event boundary covers prior admitted scopes, never a successor admission.
    if conn.execute("SELECT 1 FROM task_events s JOIN task_events o ON o.task_id=s.task_id "
                    "AND o.id<s.id WHERE s.task_id=? AND s.kind='block_requested' "
                    "AND o.kind='owner_admitted' AND o.run_id=? LIMIT 1", (task_id, run_id)).fetchone():
        return True
    blocks = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                          "AND kind IN ('blocked','block_loop_detected','dependency_wait')", (task_id, run_id))
    for block in blocks:
        try:
            payload = json.loads(block['payload'])
        except (TypeError, ValueError):
            logger.warning('kanban: unreadable terminal Stop intent for task %s run %s', task_id, run_id)
            continue
        if isinstance(payload, dict) and payload.get('supervisor_stop') is True:
            return True
    return False


def finish_aborted_owner_run(conn, task_id, run_id):
    """Post-commit cleanup for a crashed, invalidated or archived native attempt."""
    if run_id is None:
        return
    row = conn.execute('SELECT outcome FROM task_runs WHERE task_id=? AND id=?', (task_id, run_id)).fetchone()
    if row and row['outcome'] in _ABORTED_OWNER_OUTCOMES:
        interrupt_owner_run(conn, run_id)
