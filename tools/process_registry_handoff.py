"""Transfer a delegated child's live process and approval reservation to its parent."""

from typing import Optional


_MAX_HANDOFFS_PER_CHILD = 3


def handoff_process(session_id: str, args: dict, task_id: Optional[str]) -> dict:
    """Require a live child owner and parent before moving completion routing."""
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
    from tools.process_registry import process_registry
    from tools.terminal_tool import _resolve_container_task_id

    with _active_subagents_lock:
        record = _active_subagents.get(str(task_id or ""))
    child = record.get("agent") if record else None
    parent_ref = getattr(child, "_delegate_parent_ref", None)
    parent = parent_ref() if callable(parent_ref) else None
    if parent is None:
        return {"error": "handoff is only available to a running subagent with a live parent; you are not one."}
    parent_owner = str(getattr(parent, "_current_task_id", "") or getattr(parent, "session_id", "") or "")
    if not parent_owner:
        return {"error": "parent has no process owner id yet; retry after the parent's turn has started."}
    handed = getattr(child, "_handed_off_processes", None)
    if handed is None:
        handed = child._handed_off_processes = []
    if len(handed) >= _MAX_HANDOFFS_PER_CHILD:
        return {"error": f"handoff cap reached ({_MAX_HANDOFFS_PER_CHILD} per subagent); wait on or kill the rest yourself."}
    note = str(args.get("data") or "").strip()
    if not note:
        return {"error": "handoff requires `data`: one sentence saying what the process is for and what the parent should do with its result."}
    session = process_registry.transfer_ownership(
        session_id, from_owner=str(task_id or ""), to_owner=parent_owner,
        to_task_id=_resolve_container_task_id(parent_owner),
        to_session_key=str(getattr(parent, "_gateway_session_key", "") or getattr(parent, "session_id", "") or ""),
        note=note,
        child_stop_event=getattr(child, "_hard_interrupt_requested", None))
    if session is None:
        return {"error": f"cannot hand off {session_id}: process exited, ownership changed, or its approval task "
                         "was stopped; read its result with poll/log and report it instead."}
    handed.append({"session_id": session.id, "command": session.command, "note": note})
    return {"status": "handed_off", "session_id": session.id, "command": session.command,
            "note": "Your parent now owns this process and will receive its completion; you will not. Mention the handoff "
                    "in your final answer."}
