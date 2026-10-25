"""Use the hosted producer's existing private preclaim authorization verdict."""


def hosted_origin(authority, ref, row):
    from gateway.session_native_approval import native_owner_current
    from hermes_state_runtime import get_session_admission

    if not row.get('_hosted_authorized') or not row.get('request_id', '').startswith('hosted:'):
        return None
    if (row.get('_hosted_authorized') != (authority.epoch, row['admission_id'])
            or ref.profile_id != authority.profile_id or not native_owner_current(authority)):
        return None
    committed = get_session_admission(authority.db, admission_id=row['admission_id'])
    fields = ('owner_epoch', 'generation', 'target_session_id', 'principal_id', 'request_id', 'payload')
    if (committed is None or committed['owner_epoch'] != authority.epoch
            or committed['target_session_id'] != ref.session_id
            or any(committed[key] != row[key] for key in fields)):
        return None
    live = authority.sessions.get(ref.session_id)
    if live is None or live.superseded:
        return None
    return {'text': row['payload']['text'], 'identity': row['principal_id'],
            'profile': str(authority.profile_id), 'session': live.route,
            'admission': row['admission_id'], 'generation': row['generation']}


def hosted_task_scope(authority, ref, row, live, event):
    origin = hosted_origin(authority, ref, row)
    if origin is None:
        return None
    from gateway.session_ingress import _native_task_scope
    return _native_task_scope(authority, ref, row, live, event, origin['text'],
                              producer_current=lambda: hosted_origin(authority, ref, row) is not None)
