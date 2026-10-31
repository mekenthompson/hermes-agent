"""Read-only, exact-owner delivery handles. Never mount or adopt history."""
from hermes_state_runtime import RuntimeStoreError


async def delivery_handle(connection, ref, params):
    from gateway.session_authorities import require_serving
    from gateway.session_local_recovery import owned_receipt
    from gateway.owner_identity import same_owner

    authority, actor = connection.authority, connection.actor
    require_serving(authority)
    if actor.profile_id != authority.profile_id or ref.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if 'session:read' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    # Missing live ownership is a prerequisite, not permission to restore, migrate,
    # attach a viewer, refresh launch policy or follow a compression redirect.
    if ref.session_id not in authority.sessions:
        raise RuntimeStoreError('not_found')
    try:
        receipt, source, route, entry = owned_receipt(authority, ref.session_id)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeStoreError('not_found') from exc
    if not same_owner(receipt['principal_id'], actor.subject):
        raise RuntimeStoreError('permission_denied')
    live = authority.sessions[ref.session_id]
    if live.route != route or live.source != source:
        raise RuntimeStoreError('not_found')
    handle = authority._handle(ref)
    if handle.execution_state == 'unknown':
        raise RuntimeStoreError('session_unknown')
    return {'session_id': ref.session_id, 'stored_session_id': entry.session_id,
            'profile_id': authority.profile_id, 'instance_id': authority.instance_id,
            'authority_epoch': handle.authority_epoch, 'revision': handle.revision,
            'execution_generation': handle.execution_generation,
            'execution_state': handle.execution_state}
