"""Store passive Kanban checkpoints as display-only conversation rows."""

import time

from hermes_state_messages import _INSERT_MESSAGE_SQL


def persist_local_checkpoint(db, session_id: str, key: str, body: str, *, board: str, task_id: str) -> int:
    """Return the stable row for a native event; never add it to model history."""
    target = db.resolve_resume_session_id(session_id)
    lineage = db._resume_lineage_ids(target)
    message = {"role": "session_meta", "content": body, "display_kind": "kanban_checkpoint",
               "platform_message_id": key, "display_metadata": {"board": board, "task_id": task_id}}
    params = db._message_row_params(target, "session_meta", message, None, time.time(), keep_reasoning=False)

    def write(conn):
        db._check_transcript_write_guards(conn, target, None)
        prior = conn.execute(
            "SELECT id, content FROM messages WHERE session_id IN (" + ",".join("?" for _ in lineage) + ") "
            "AND role = 'session_meta' AND display_kind = 'kanban_checkpoint' "
            "AND platform_message_id = ? ORDER BY id LIMIT 1",
            (*lineage, key),
        ).fetchone()
        if prior is not None:
            if db._decode_content(prior["content"]) != body:
                raise ValueError("Native checkpoint identity conflicts with stored conversation text")
            return int(prior["id"])
        row_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid
        db._bump_session_counters(conn, target, 1, 0, unit=True)
        return int(row_id)

    return db._execute_write(write, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)
