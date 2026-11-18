"""Owner-side delivery reconciliation for adapters with durable provider inboxes.

Provider IDs belong to the authenticated source, across its conversation resets.
Existing native rows and deletion tombstones are the only receipt store.
"""
import json

from gateway.session_persistence import authority_persistence
from persistence.authority.vocabulary import admission_fingerprint
from persistence.core.errors import AuthorityRefusal


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
    from gateway.session_authority import admission_row
    from gateway.session_envelope import restore_native_source
    from gateway.session_ingress_media import reconcile_native_retry
    source = restore_native_source(payload)
    if getattr(authority.runner._adapter_for_source(source), 'requires_durable_input_ack', False) is not True:
        return None
    require_serving(authority)
    envelope = payload['native_text_v1']
    delegated = (envelope.get('reauthorize') == 'roles' or 'relay' in envelope.get('provenance', {})
        or authority.runner._adapter_flag(source.platform, 'authorization_is_upstream',
                                         authority.runner._adapter_profile_for_source(source)))
    if not authority.runner._is_user_authorized_for_source(source, allow_adapter_delegation=delegated):
        raise AuthorityRefusal('permission_denied')
    if not request:
        raise AuthorityRefusal('invalid_params', field='message_id')
    store = authority_persistence(authority)
    receipts = store.admissions.native_receipts(epoch=authority.epoch, principal_id=principal, request_id=request)
    if not receipts:
        return None
    if len(receipts) != 1:
        raise AuthorityRefusal('admission_conflict')
    record, digest = receipts[0]
    row = admission_row(record)
    compared = reconcile_native_retry(store, principal_id=principal, session_id=row['target_session_id'],
        request_id=request, payload=payload, raw=raw)
    if control := row['payload'].get('native_stop_v1'):
        compared = {**compared, 'native_stop_v1': control}
    compared_digest = admission_fingerprint(canonical_target=row['target_session_id'],
        payload={'input': compared, 'intent': str(row['intent'])})
    if compared_digest != digest:
        raise AuthorityRefusal('admission_conflict')
    return row


def before_route(authority, principal, request, payload, raw=None):
    """A terminal retry needs no route, history mount, model or Stop execution."""
    row = find(authority, principal, request, payload, raw)
    if row is None or row['status'] == 'terminal':
        return row
    entry = authority.runner.session_store.lookup_by_session_key(payload['native_text_v1']['route'])
    if entry is None or authority.logical_owner(entry.session_id) != row['target_session_id']:
        raise AuthorityRefusal('session_superseded')
    return None  # normal admission reconciles a pending receipt on its original route
