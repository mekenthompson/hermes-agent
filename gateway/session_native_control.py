"""Typed native Stop admission over the existing input rows and process Stop fence.

Only the native codec supplies a control callback. Payloads submitted through
ordinary APIs cannot create this descriptor or exempt work from FIFO capacity.
"""
from functools import partial

from gateway.session_contract import SessionRef
from hermes_state_runtime import (
    RuntimeStoreError, consume_native_stop, get_session_admission, list_session_admissions,
)


def snapshot(authority, ref, conn):
    """Capture target/cutoff and persist the existing Stop fence in admission's write."""
    session = conn.execute('SELECT runtime_generation FROM sessions WHERE id=?', (ref.session_id,)).fetchone()
    if session is None:
        raise RuntimeStoreError('not_found')
    cutoff = conn.execute('SELECT COALESCE(MAX(seq),0) FROM session_admissions WHERE target_session_id=?',
                          (ref.session_id,)).fetchone()[0]
    paused = conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? AND status='unknown' LIMIT 1",
                          (ref.session_id,)).fetchone() is not None
    from gateway.session_ancillary import record_process_stop
    record_process_stop(authority, ref, session[0], cursor=conn)
    if not paused:
        conn.execute("UPDATE session_admissions SET status='terminal',outcome='cancelled' "
                     "WHERE target_session_id=? AND seq<=? AND status='queued' AND principal_id LIKE 'messaging:%' "
                     "AND json_type(payload_json,'$.native_stop_v1') IS NULL", (ref.session_id, cutoff))
    return {'kind': 'stop', 'generation': session[0], 'cutoff': cutoff, 'paused': paused}


def _descriptor(row):
    value = row['payload'].get('native_stop_v1')
    if (not isinstance(value, dict) or set(value) != {'kind', 'generation', 'cutoff', 'paused'}
            or value['kind'] != 'stop' or type(value['paused']) is not bool
            or any(type(value[key]) is not int or value[key] < 0 for key in ('generation', 'cutoff'))
            or row['payload'].get('native_text_v1', {}).get('event', {}).get('structured_control') != 'stop'):
        raise RuntimeStoreError('invalid_params', field='native_control')
    return value


async def _apply(authority, ref, row):
    from gateway.session_envelope import check_native_route
    from gateway.session_runtime_workers import tracked_write
    from gateway.session_managed_worker import interrupt_managed_owned
    control = _descriptor(row)
    live = authority.sessions[ref.session_id]
    # Recheck the original actor/transport, never a later message's author or a
    # forwarded caller label. An invalid control retains its pending Stop fence.
    await check_native_route(authority.runner, row['payload'], authority.physical_target(ref), live.source,
                             authority.runner._adapter_for_source(live.source))
    from hermes_state_runtime import _epoch
    from gateway.session_authorities import require_serving
    require_serving(authority)
    with authority.db._read_ctx() as conn:
        _epoch(conn, authority.epoch)
    try:
        if not interrupt_managed_owned(authority, ref, control['generation']):
            await authority._interrupt_owned(ref, control['generation'])
    except RuntimeStoreError as exc:
        if exc.reason not in {'stale_generation', 'process_stop_unknown'}:
            raise
        # Stale target is never replaced with the current one. Unknown cleanup
        # keeps the existing durable fence and remains distinct from acceptance.
    await tracked_write(authority, partial(consume_native_stop, authority.db,
        epoch=authority.epoch, admission_id=row['admission_id']))
    if not control['paused']:
        for cancelled in list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False):
            if (cancelled['seq'] <= control['cutoff'] and cancelled['principal_id'].startswith('messaging:')
                    and (cancelled['status'], cancelled['outcome']) == ('terminal', 'cancelled')
                    and 'native_stop_v1' not in cancelled['payload']):
                authority._settle_cancelled(ref, cancelled['admission_id'])


async def reconcile(authority, ref):
    """Process pending controls before any model claim, regardless of their FIFO seq."""
    live = authority.sessions.get(ref.session_id)
    if live is None:
        raise RuntimeStoreError('not_found')
    async with live.claim_gate:
        rows = list_session_admissions(authority.db, session_id=ref.session_id)
        for row in rows:
            if row['status'] != 'queued' or 'native_stop_v1' not in row['payload']:
                continue
            current = get_session_admission(authority.db, admission_id=row['admission_id'])
            if current is not None and current['status'] == 'queued':
                await _apply(authority, SessionRef(authority.profile_id, ref.session_id), current)
