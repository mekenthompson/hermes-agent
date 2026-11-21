"""House: store passive Kanban checkpoints as display-only conversation rows."""

import time

from .messages import (
    _bump_session_counters,
    _check_transcript_write_guards,
    _decode_content,
    _message_row_params,
    _resume_lineage_ids,
    resolve_resume_session_id,
)
from .messages_sql import _INSERT_MESSAGE_SQL

DISPLAY_CHECKPOINT_KIND = "kanban_checkpoint"


def persist_local_checkpoint(holder, session_id: str, key: str, body: str, *, board: str, task_id: str) -> int:
    """Return the stable row for a native event; never add it to model history."""
    target = resolve_resume_session_id(holder, session_id)
    lineage = _resume_lineage_ids(holder, target)
    message = {"role": "session_meta", "content": body, "display_kind": DISPLAY_CHECKPOINT_KIND,
               "platform_message_id": key, "display_metadata": {"board": board, "task_id": task_id}}
    params = _message_row_params(holder, target, "session_meta", message, None, time.time(), keep_reasoning=False)

    def write(conn):
        _check_transcript_write_guards(holder, conn, target, None)
        prior = conn.execute(
            "SELECT id, content FROM messages WHERE session_id IN (" + ",".join("?" for _ in lineage) + ") "
            "AND role = 'session_meta' AND display_kind = ? AND platform_message_id = ? ORDER BY id LIMIT 1",
            (*lineage, DISPLAY_CHECKPOINT_KIND, key),
        ).fetchone()
        if prior is not None:
            if _decode_content(prior["content"]) != body:
                raise ValueError("Native checkpoint identity conflicts with stored conversation text")
            return int(prior["id"])
        row_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid
        _bump_session_counters(conn, target, 1, 0, unit=True)
        return int(row_id)

    return holder._execute_write(write, patience_s=holder._TRANSCRIPT_WRITE_PATIENCE_S)
