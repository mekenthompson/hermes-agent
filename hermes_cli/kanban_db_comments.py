"""Native task comments and explicit human checkpoints."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional


def add_comment(conn: sqlite3.Connection, task_id: str, author: str, body: str, *,
                human_checkpoint: bool = False, request_id: Optional[str] = None) -> int:
    from hermes_cli import kanban_db as kb

    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    if human_checkpoint:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError("human checkpoint requires a nonempty request_id of at most 200 characters")
    elif request_id is not None:
        raise ValueError("request_id requires human_checkpoint")
    now = int(kb.time.time())
    # Graph builders compose ordinary comment writes under one outer commit.
    with kb.write_txn(conn, allow_nested=True):
        kb._require_task(conn, task_id)
        if human_checkpoint:
            for row in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'checkpoint' ORDER BY id",
                (task_id,),
            ):
                prior = json.loads(row["payload"] or "{}")
                if prior.get("request_id") != request_id:
                    continue
                if prior.get("author") != author.strip() or prior.get("body") != body:
                    raise ValueError("checkpoint request_id was already used for different content")
                return int(prior["comment_id"])
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)", (task_id, author.strip(), body if human_checkpoint else body.strip(), now),
        )
        if human_checkpoint:
            kb._append_event(conn, task_id, "checkpoint", {"author": author.strip(), "body": body,
                                                         "comment_id": int(cur.lastrowid), "request_id": request_id})
        else:
            kb._append_event(conn, task_id, "commented", {"author": author, "len": len(body)})
        return int(cur.lastrowid or 0)
