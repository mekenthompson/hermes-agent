"""House ``notice`` mutation: an operator notice and its canonical retry receipt commit together, without inference."""

from ..conversation._store.messages import _bump_session_counters, _check_transcript_write_guards, _insert_message_rows
from ._local import local_physical_target
from ._runtime_guards import require_idle


def notice_in_transaction(holder, conn, session_id, payload):
    """Append the notice to the idle conversation's current transcript; ``(affected, projection)``."""
    target = local_physical_target(conn, session_id)
    require_idle(holder, conn, list({session_id, target}))
    _check_transcript_write_guards(holder, conn, target, None, reject_active_turn_lease=True)
    message = {'role': 'user', 'content': payload['text'], 'display_kind': 'internal_notification',
               'display_metadata': {'display_text': payload['text'], 'notification_category': 'operation'}}
    _insert_message_rows(holder, conn, target, [message])
    _bump_session_counters(conn, target, 1, 0, unit=True)
    return {session_id, target}, {'notice_row_id': message['_row_id'],
                                  'notice_message_uid': message['message_uid'], 'text': payload['text']}
