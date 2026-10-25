"""Resolve a native creation request without replaying archived or ambiguous work."""


def existing_task(conn, request_id):
    if not request_id:
        return None
    rows = conn.execute("SELECT id FROM tasks WHERE idempotency_key=? ORDER BY created_at, id LIMIT 2",
                        (request_id,)).fetchall()
    if len(rows) > 1:
        raise ValueError("ambiguous task idempotency_key; reconcile existing tasks before retrying")
    return rows[0]["id"] if rows else None
