"""Owner-side delivery reconciliation for adapters with durable provider inboxes.

Provider IDs belong to the authenticated source, across its conversation resets.
Existing native rows and deletion tombstones are the only receipt store.
"""
import json

from hermes_state_runtime import RuntimeStoreError, _epoch, _row, admission_fingerprint


def identity(source, payload):
    principal = 'messaging:' + json.dumps([source.profile, source.platform.value, source.chat_id,
        source.thread_id, source.user_id], separators=(',', ':'))
    event = payload['native_text_v1']['event']
    request = str(event['message_id'] or '')
    if revision := event.get('edit_revision'):
        request += ':edit:' + revision
    return principal, request


def find(authority, principal, request, payload, raw=None):
    """Exact receipt or None; ambiguity/changed content cannot reserve today's route."""
    from gateway.session_authorities import require_serving
    from gateway.session_envelope import restore_native_source
    from gateway.session_ingress_media import reconcile_native_retry
    from hermes_state_terminal import ADMISSION_PREFIX
    source = restore_native_source(payload)
    if getattr(authority.runner._adapter_for_source(source), 'requires_durable_input_ack', False) is not True:
        return None
    require_serving(authority)
    envelope = payload['native_text_v1']
    delegated = (envelope.get('reauthorize') == 'roles' or 'relay' in envelope.get('provenance', {})
        or authority.runner._adapter_flag(source.platform, 'authorization_is_upstream',
                                         authority.runner._adapter_profile_for_source(source)))
    if not authority.runner._is_user_authorized_for_source(source, allow_adapter_delegation=delegated):
        raise RuntimeStoreError('permission_denied')
    if not request:
        raise RuntimeStoreError('invalid_params', field='message_id')
    with authority.db._read_ctx() as conn:
        _epoch(conn, authority.epoch)
        rows = [dict(row) for row in conn.execute('SELECT * FROM session_admissions '
            'WHERE principal_id=? AND request_id=?', (principal, request))]
        rows += [json.loads(row[0]) for row in conn.execute("SELECT value FROM state_meta WHERE key GLOB ? "
            "AND json_extract(value,'$.principal_id')=? AND json_extract(value,'$.request_id')=?",
            (ADMISSION_PREFIX + '*', principal, request))]
    rows = {row['admission_id']: row for row in rows}
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeStoreError('admission_conflict')
    saved = next(iter(rows.values()))
    digest = saved['payload_digest']
    row = _row(saved)
    compared = reconcile_native_retry(authority.db, principal_id=principal, session_id=row['target_session_id'],
        request_id=request, payload=payload, raw=raw)
    if control := row['payload'].get('native_stop_v1'):
        compared = {**compared, 'native_stop_v1': control}
    compared_digest = admission_fingerprint(canonical_target=row['target_session_id'],
        payload={'input': compared, 'intent': row['intent']})
    if compared_digest != digest:
        raise RuntimeStoreError('admission_conflict')
    return row


def before_route(authority, principal, request, payload, raw=None):
    """A terminal retry needs no route, history mount, model or Stop execution."""
    row = find(authority, principal, request, payload, raw)
    if row is None or row['status'] == 'terminal':
        return row
    entry = authority.runner.session_store.lookup_by_session_key(payload['native_text_v1']['route'])
    if entry is None or authority.logical_owner(entry.session_id) != row['target_session_id']:
        raise RuntimeStoreError('session_superseded')
    return None  # normal admission reconciles a pending receipt on its original route
