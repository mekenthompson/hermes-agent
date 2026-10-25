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
    if (row['status'] not in {'started', 'terminal'} or type(row['generation']) is not int
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
    marker = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                          "AND kind='owner_admitted' ORDER BY id DESC LIMIT 1",
                          (run['task_id'], run_id)).fetchone()
    if marker is None:
        return None
    payload = {'status': 'unknown', 'reason': 'invalid_owner_marker'}
    try:
        if conn.in_transaction:
            raise ValueError('native Stop requires a committed supervisor block')
        binding = json.loads(marker['payload'])
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
            kb._append_event(conn, run['task_id'], 'native_owner_stop',
                {'status': 'pending', 'session_id': binding['session_id'],
                 'request_id': binding['request_id']}, run_id=run_id)
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
    """The existing reclaim/timeout may end its submitter only after native cleanup."""
    return run_id is None or interrupt_owner_run(conn, run_id, allow_running=True) is not False


def finish_aborted_owner_run(conn, task_id, run_id):
    """Post-commit cleanup for a crashed, invalidated or archived native attempt."""
    if run_id is None:
        return
    row = conn.execute('SELECT outcome FROM task_runs WHERE task_id=? AND id=?', (task_id, run_id)).fetchone()
    if row and row['outcome'] in {'reclaimed', 'stale', 'timed_out', 'crashed', 'gave_up', 'spawn_failed'}:
        interrupt_owner_run(conn, run_id)
