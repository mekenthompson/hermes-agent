"""Bounded native command authority using the session's existing Stop leases."""
from contextlib import nullcontext
import json
import uuid
from weakref import WeakSet


def command_task_scope(authority, ref, identity, text, arrival_epoch, *, producer_current=None):
    from tools.approval_context import (approval_task_failure_scope, approval_task_revoked,
                                        current_approval_task_context)
    if approval_task_revoked() or current_approval_task_context() is not None:
        return approval_task_failure_scope(nullcontext())  # never replace or broaden inherited authority
    return _native_command_task_scope(authority, ref, identity, text, arrival_epoch,
                                       producer_current=producer_current)


def _native_command_task_scope(authority, ref, identity, text, arrival_epoch, *, producer_current=None):
    """Construct a lease only after a private native producer validated its own unit of work."""
    from gateway.session_authorities import require_serving
    from hermes_state_runtime import RuntimeStoreError, _epoch
    from tools.approval_context import approval_task_context, approval_task_failure_scope
    live = authority.sessions[ref.session_id]

    def current():
        if producer_current is not None and not producer_current():
            return False
        entry = authority.runner.session_store.lookup_by_session_key(live.route)
        owner = authority.logical_owner(entry.session_id) if entry is not None else None
        try:
            require_serving(authority)
            with authority.db._read_ctx() as conn:
                _epoch(conn, authority.epoch)
        except RuntimeStoreError:
            return False
        return (authority.sessions.get(ref.session_id) is live and not live.superseded
                and authority.route_epoch(live.route) == arrival_epoch
                and entry is not None and not entry.suspended and owner == ref.session_id)

    def own(lease):
        with live.event_stream.lock:
            if not hasattr(live, "approval_task_leases"):
                live.approval_task_leases = WeakSet()
            live.approval_task_leases.add(lease)
            if not current():
                lease.revoke()

    return approval_task_failure_scope(approval_task_context(text, identity=identity, profile=str(authority.profile_id),
        session=live.route, task_id="command:" + uuid.uuid4().hex, run_id=str(arrival_epoch),
        is_current=current, owner=own))


async def messaging_command_scope(runner, event, route, arrival_epoch):
    """Only the adapter's exact private native callback can authorize an off-turn command."""
    from gateway.session_authorities import active_authority
    from gateway.session_contract import Principal, SessionRef
    from gateway.session_envelope import _adapter_event, _validate_native
    from gateway.session_ingress_context import capture_provenance, reauthorize_roles

    authority = active_authority(runner)
    if authority is None or event.internal or not event.allow_gateway_control:
        return nullcontext()
    provenance = capture_provenance(runner, event)
    if provenance is None:
        return nullcontext()
    frozen = _adapter_event(event)
    fresh_roles = await reauthorize_roles(runner, frozen.source, provenance)
    _validate_native(runner, frozen, provenance, fresh_roles, allow_command=True)
    source = frozen.source
    entry = runner.session_store.lookup_by_session_key(route)
    owner = authority.logical_owner(entry.session_id) if entry is not None else None
    if owner not in authority.sessions:
        from functools import partial
        from gateway.session_runtime_workers import tracked_write
        ref = await tracked_write(authority, partial(authority._reserve_route, source),
                                  then=lambda reserved: authority._publish_route(source, *reserved))
        fresh_roles = await reauthorize_roles(runner, source, provenance)
        _validate_native(runner, frozen, provenance, fresh_roles, allow_command=True)
    else:
        ref = SessionRef(authority.profile_id, owner)
    identity = "messaging:" + json.dumps([source.profile, source.platform.value, source.chat_id,
                                          source.thread_id, source.user_id], separators=(",", ":"))
    actor = Principal(identity, authority.profile_id, frozenset({"session:control"}), "native-command")
    authority.authorize(actor, ref, "session:control")
    return command_task_scope(authority, ref, identity, event.text, arrival_epoch)
