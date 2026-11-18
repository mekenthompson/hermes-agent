"""House native controls on SQLite: the typed native Stop row, its watermark and the session process-Stop fence.

A native Stop is an ordinary ``session_admissions`` row whose payload carries the owner-produced
``native_stop_v1`` descriptor. Only the owner's native codec asks ``admit`` to build one (``native_stop``), so a
payload submitted through ``prompt.submit`` can never create the descriptor or exempt work from the FIFO cap. The
process-Stop fence is one ``state_meta`` key per session written in the same transaction as the Stop row.
"""

from __future__ import annotations

import json

from persistence.core.errors import AuthorityRefusal

from ._runtime import _CANCEL_QUEUED, _admission, _epoch, _row

__all__ = [
    "NATIVE_STOP_KEY",
    "PROCESS_STOP_PREFIX",
    "check_native_stop_fence",
    "check_epoch",
    "clear_process_stop",
    "consume_native_stop",
    "has_native_stop",
    "interrupt_steer_input",
    "native_stop_snapshot",
    "native_stop_watermark",
    "process_stop_pending",
    "record_process_stop",
]

NATIVE_STOP_KEY = 'native_stop_v1'
PROCESS_STOP_PREFIX = 'gateway.process_stop.v1:'
# A queued row carrying the typed descriptor; ordinary model input never does.
CONTROL_ROW = "json_type(payload_json, '$.native_stop_v1')='object'"
ORDINARY_ROW = "json_type(payload_json, '$.native_stop_v1') IS NULL"


def has_native_stop(payload) -> bool:
    return NATIVE_STOP_KEY in payload


def process_stop_pending(holder, session_id):
    """Native cleanup status only; process ownership remains in the existing registry."""
    return holder.get_meta(PROCESS_STOP_PREFIX + session_id)


def _record_process_stop(conn, session_id, token):
    pending = conn.execute('SELECT value FROM state_meta WHERE key=?', (PROCESS_STOP_PREFIX + session_id,)).fetchone()
    if pending is not None:
        return pending[0]
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (PROCESS_STOP_PREFIX + session_id, token))
    return token


def record_process_stop(holder, *, epoch, session_id, generation):
    """Persist the session's process-Stop fence (first writer wins); returns the pending token."""
    token = f'{epoch}:{generation}'

    def write(conn):
        _epoch(conn, epoch)
        return _record_process_stop(conn, session_id, token)
    return holder._execute_write(write)


def clear_process_stop(holder, *, epoch, session_id, token):
    def write(conn):
        _epoch(conn, epoch)
        conn.execute('DELETE FROM state_meta WHERE key=? AND value=?', (PROCESS_STOP_PREFIX + session_id, token))
    holder._execute_write(write)


def check_epoch(holder, *, epoch, reopen=True):
    """Refuse ``stale_epoch`` unless ``epoch`` still owns the store."""
    with holder._read_ctx(reopen=reopen) as conn:
        _epoch(conn, epoch)


def native_stop_snapshot(conn, *, epoch, session_id):
    """Inside ``admit``'s write: capture target/cutoff, persist the process-Stop fence, and cancel the earlier
    queued messaging input this Stop supersedes (unless the FIFO is paused behind an unknown turn)."""
    session = conn.execute('SELECT runtime_generation FROM sessions WHERE id=?', (session_id,)).fetchone()
    if session is None:
        raise AuthorityRefusal('not_found')
    cutoff = conn.execute('SELECT COALESCE(MAX(seq),0) FROM session_admissions WHERE target_session_id=?',
                          (session_id,)).fetchone()[0]
    paused = conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? AND status='unknown' LIMIT 1",
                          (session_id,)).fetchone() is not None
    _record_process_stop(conn, session_id, f'{epoch}:{session[0]}')
    if not paused:
        conn.execute("UPDATE session_admissions SET status='terminal',outcome='cancelled' "
                     "WHERE target_session_id=? AND seq<=? AND status='queued' AND principal_id LIKE 'messaging:%' "
                     f"AND {ORDINARY_ROW}", (session_id, cutoff))
    return {'kind': 'stop', 'generation': session[0], 'cutoff': cutoff, 'paused': paused}


def native_stop_watermark(holder, session_id=None, *, cursor=None, route=None):
    """Existing control rows are the persistent fence for in-flight native capture."""
    if not session_id and route is None:
        return 0
    selector, value = ('target_session_id', session_id) if route is None else (
        "json_extract(payload_json, '$.native_text_v1.route')", route)

    def read(conn):
        return conn.execute(f"SELECT COALESCE(MAX(seq),0) FROM session_admissions WHERE {selector}=? "
                            f"AND {CONTROL_ROW}", (value,)).fetchone()[0]
    if cursor is not None:
        return read(cursor)
    with holder._read_ctx() as conn:
        return read(conn)


def check_native_stop_fence(conn, fence):
    """``admit``'s in-write check that no native Stop of ``fence.route`` committed after the input arrived."""
    if fence is not None and native_stop_watermark(None, cursor=conn, route=fence.route) != fence.seen:
        raise AuthorityRefusal('stale_generation', field='route',
                               detail='a /stop or /new of this chat came after this message')


def consume_native_stop(holder, *, epoch, admission_id):
    """Consume only a committed typed control; this does not prove physical cleanup."""
    def write(conn):
        _epoch(conn, epoch)
        row = _admission(conn, admission_id)
        if not isinstance(json.loads(row['payload_json']).get(NATIVE_STOP_KEY), dict):
            raise AuthorityRefusal('invalid_params')
        conn.execute(_CANCEL_QUEUED + " AND status='queued'", (admission_id,))
        return _row(_admission(conn, admission_id))
    return holder._execute_write(write)


PROCESS_STOP_PRINCIPAL = 'native-process-stop'


def _completion_cancelled(conn, owner, identity, process_id=None):
    if identity is None:
        # Legacy/malformed events cannot claim a new incarnation of a producer
        # whose native owner already cancelled it. No result-cache fallback.
        return conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? AND principal_id=? "
            "AND status='terminal' AND outcome='cancelled' AND "
            "json_extract(CASE WHEN json_valid(request_id) THEN request_id ELSE '[]' END,'$[1]')=?",
            (owner, PROCESS_STOP_PRINCIPAL, process_id)).fetchone() is not None
    return conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? "
        "AND principal_id=? AND request_id=? AND status='terminal' AND outcome='cancelled'",
        (owner, PROCESS_STOP_PRINCIPAL, identity)).fetchone() is not None


def completion_cancelled(holder, *, session_id, identity=None, process_id=None) -> bool:
    """Whether the session's native owner cancelled this process completion (or, without an identity, any
    incarnation of ``process_id``)."""
    with holder._read_ctx() as conn:
        return _completion_cancelled(conn, session_id, identity, process_id)


def check_completion_guard(conn, session_id, identities):
    """``admit``'s in-write check: none of the completion ``identities`` was cancelled by a native Stop and no
    process Stop is pending on the session (``process_result_consumed``)."""
    if not identities:
        return
    if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (PROCESS_STOP_PREFIX + session_id,)).fetchone():
        raise AuthorityRefusal('process_result_consumed')
    for identity in identities:
        if _completion_cancelled(conn, session_id, identity):
            raise AuthorityRefusal('process_result_consumed')


def cancel_process_completions(holder, *, epoch, session_id, completions):
    """Commit terminal producer cancellations, never queued input or output history.

    ``completions`` are ``(identity, route)`` pairs: the canonical process producer identity and its route.
    These records cannot execute or add a prompt; admission retirement handles their lifetime with the owning
    conversation."""
    import uuid
    from persistence.authority.vocabulary import admission_fingerprint
    from ._runtime import _json, _session

    def write(conn):
        _epoch(conn, epoch)
        _session(conn, session_id)
        for identity, route in completions:
            payload = {'process_completion': identity, 'route': route}
            digest = admission_fingerprint(canonical_target=session_id, payload={'input': payload, 'intent': 'queue'})
            conn.execute("INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,"
                "lineage_json,payload_json,payload_digest,intent,status,outcome,owner_epoch) "
                "VALUES(?,?,?,?,?,?,?,'queue','terminal','cancelled',?) "
                "ON CONFLICT(principal_id,target_session_id,request_id) DO NOTHING",
                (uuid.uuid4().hex, identity, PROCESS_STOP_PRINCIPAL, session_id,
                 json.dumps([session_id]), _json(payload), digest, epoch))
            if not _completion_cancelled(conn, session_id, identity):
                raise AuthorityRefusal('process_stop_unknown')
    holder._execute_write(write)


def native_receipts(holder, *, epoch, principal_id, request_id):
    """Every live row and retired tombstone of one provider identity across the source's conversation
    resets, as ``(row, payload_digest)``; the caller decides ambiguity and replay."""
    from ._runtime import ADMISSION_PREFIX
    with holder._read_ctx() as conn:
        _epoch(conn, epoch)
        rows = [dict(row) for row in conn.execute('SELECT * FROM session_admissions '
            'WHERE principal_id=? AND request_id=?', (principal_id, request_id))]
        rows += [json.loads(row[0]) for row in conn.execute("SELECT value FROM state_meta WHERE key GLOB ? "
            "AND json_extract(value,'$.principal_id')=? AND json_extract(value,'$.request_id')=?",
            (ADMISSION_PREFIX + '*', principal_id, request_id))]
    unique = {row['admission_id']: row for row in rows}
    return [(_row(row), row['payload_digest']) for row in unique.values()]


def interrupt_steer_input(holder, *, epoch: int, admission_id: str, generation: int) -> bool:
    """An attempted/Stopped transfer must never become another queued execution."""
    def write(conn):
        _epoch(conn, epoch)
        return conn.execute("UPDATE session_admissions SET outcome='interrupted' WHERE admission_id=? "
                            "AND status='terminal' AND outcome IN ('steering','steered') "
                            "AND owner_epoch=? AND generation=?",
                            (admission_id, epoch, generation)).rowcount == 1
    return holder._execute_write(write)
