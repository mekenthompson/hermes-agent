"""Small owner-side projection of actual execution frames, without tool arguments."""


def execution_info(authority, ref):
    """Keep the session.info envelope while skipping tool/skill/MCP inventory."""
    from gateway.session_local import local_session_info
    from hermes_state_runtime import RuntimeStoreError

    if ref.session_id not in authority.sessions:
        raise RuntimeStoreError('not_found')
    info = local_session_info(authority, ref)
    keys = ('source', 'lazy', 'profile_id', 'desktop_protocol', 'profile_name')
    return {**{key: info[key] for key in keys},
            'execution_progress': execution_progress(authority, ref)}


def execution_progress(authority, ref):
    """Project a running tool after the caller has authorized session:read.

    Inventory polling must not attach a viewer or construct an agent. A paused,
    stopped or replaced execution cannot lend its last tool to another turn.
    """
    from gateway.session_ancillary import process_stop_pending

    live = authority.sessions.get(ref.session_id)
    if live is None:
        return None
    handle = authority._handle(ref)
    generation = handle.execution_generation
    if (handle.execution_state != 'running'
            or authority.pending_stops.get(ref.session_id) == generation
            or process_stop_pending(authority.db, ref.session_id) is not None):
        return None
    with live.event_stream.lock:
        if (authority.executing.get(ref.session_id) != generation
                or live.controls.snapshot(ref.session_id, generation)):
            return None
        progress = live.event_stream.progress_for(generation)
        current = authority._handle(ref)
        if (not progress.get('tool') or current.execution_state != 'running'
                or current.execution_generation != generation
                or authority.executing.get(ref.session_id) != generation):
            return None
        return {'execution_generation': generation, 'tool': progress['tool'],
                'progress_at': progress['progress_at']}
