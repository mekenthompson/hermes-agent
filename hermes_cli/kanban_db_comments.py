"""Atomic native task comments and idempotent follow-up delivery."""
import hashlib
import json
import time


def add_comment(conn, task_id, author, body, *, idempotency_key=None):
    from hermes_cli import kanban_db as kb

    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 512):
        raise ValueError("comment idempotency_key must be a nonempty string of at most 512 characters")
    fingerprint = hashlib.sha256(json.dumps([author.strip(), body.strip()]).encode()).hexdigest()
    # The comment and its existing task event commit together. Concurrent retries
    # and a lost response find this same event, without another ownership ledger.
    with kb.write_txn(conn, allow_nested=True):
        kb._require_task(conn, task_id)
        if idempotency_key is not None:
            row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='commented' "
                               "AND json_extract(payload, '$.idempotency_key')=? ORDER BY id LIMIT 1",
                               (task_id, idempotency_key)).fetchone()
            if row:
                saved = json.loads(row["payload"])
                if saved["body_sha256"] != fingerprint:
                    raise ValueError("comment idempotency_key conflicts with its original instruction")
                return saved["comment_id"]
        cur = conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                           (task_id, author.strip(), body.strip(), int(time.time())))
        comment_id = int(cur.lastrowid or 0)
        payload = {"author": author, "len": len(body)}
        if idempotency_key is not None:
            payload.update(idempotency_key=idempotency_key, body_sha256=fingerprint, comment_id=comment_id)
        kb._append_event(conn, task_id, "commented", payload)
        return comment_id
