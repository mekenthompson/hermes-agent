"""Typed native Stop admission over the existing input rows and process Stop fence.

Only the native codec asks the store for a control row (``admissions.admit(native_stop=True)``): the store
captures the target generation and FIFO cutoff, persists the process Stop fence and cancels the superseded
queued messaging input inside the admission's own write. Payloads submitted through ordinary APIs cannot
create this descriptor or exempt work from FIFO capacity.
"""
import asyncio
from functools import partial

from gateway.session_contract import SessionRef
from gateway.session_persistence import authority_persistence
from persistence.core.errors import AuthorityRefusal


def _descriptor(row):
    value = row['payload'].get('native_stop_v1')
    if (not isinstance(value, dict) or set(value) != {'kind', 'generation', 'cutoff', 'paused'}
            or value['kind'] != 'stop' or type(value['paused']) is not bool
            or any(type(value[key]) is not int or value[key] < 0 for key in ('generation', 'cutoff'))
            or row['payload'].get('native_text_v1', {}).get('event', {}).get('structured_control') != 'stop'):
        raise AuthorityRefusal('invalid_params', field='native_control')
    return value


async def _apply(authority, ref, row):
    from gateway.session_authorities import require_serving
    from gateway.session_envelope import check_native_route
    from gateway.session_managed_worker import interrupt_managed_owned
    from gateway.session_runtime_workers import tracked_write
    control = _descriptor(row)
    live = authority.sessions[ref.session_id]
    # Recheck the original actor/transport, never a later message's author or a
    # forwarded caller label. An invalid control retains its pending Stop fence.
    await check_native_route(authority.runner, row['payload'], authority.physical_target(ref), live.source,
                             authority.runner._adapter_for_source(live.source))
    require_serving(authority)
    store = authority_persistence(authority)
    await asyncio.to_thread(store.admissions.check_epoch, epoch=authority.epoch)
    try:
        if not interrupt_managed_owned(authority, ref, control['generation']):
            await authority._interrupt_owned(ref, control['generation'])
    except AuthorityRefusal as exc:
        if exc.reason not in {'stale_generation', 'process_stop_unknown'}:
            raise
        # Stale target is never replaced with the current one. Unknown cleanup
        # keeps the existing durable fence and remains distinct from acceptance.
    await tracked_write(authority, partial(store.admissions.consume_native_stop,
        epoch=authority.epoch, admission_id=row['admission_id']))
    if not control['paused']:
        rows = await asyncio.to_thread(store.admissions.list, session_id=ref.session_id, pending_only=False)
        for cancelled in rows:
            if (cancelled.seq <= control['cutoff'] and cancelled.principal_id.startswith('messaging:')
                    and (cancelled.status, cancelled.outcome) == ('terminal', 'cancelled')
                    and 'native_stop_v1' not in cancelled.payload):
                authority._settle_cancelled(ref, cancelled.admission_id)


async def reconcile(authority, ref):
    """Process pending controls before any model claim, regardless of their FIFO seq."""
    from gateway.session_authority import admission_row
    live = authority.sessions.get(ref.session_id)
    if live is None:
        raise AuthorityRefusal('not_found')
    store = authority_persistence(authority)
    async with live.claim_gate:
        # Off the owner loop, like the claim reads (tests/gateway/test_sessiondb_loop_guard.py).
        rows = await asyncio.to_thread(store.admissions.list, session_id=ref.session_id)
        for row in rows:
            if row.status != 'queued' or 'native_stop_v1' not in row.payload:
                continue
            current = await asyncio.to_thread(store.admissions.get, admission_id=row.admission_id)
            if current is not None and current.status == 'queued':
                await _apply(authority, SessionRef(authority.profile_id, ref.session_id), admission_row(current))
