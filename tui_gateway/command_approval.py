"""Off-turn commands use the existing authenticated Desktop identity and Stop leases."""
from contextlib import nullcontext
from weakref import WeakSet


def command_scope(server, sid, session, text):
    from tools.approval_context import (approval_task_context, approval_task_failure_scope,
                                        approval_task_revoked, current_approval_task_context)

    if approval_task_revoked() or current_approval_task_context() is not None:
        return approval_task_failure_scope(nullcontext())
    origin = server._approval_origin_for_direct_submit(session, text, text,
        internal_hosted_submit=False, raw_author=None, display_kind=None, turn_isolation=False, queued=True)
    if origin is None:
        return nullcontext()
    requester, request_id, _, _, generation = origin
    transport_owner, profile_home = session.get("transport_owner"), session.get("profile_home")

    def current_locked():
        return (server._sessions.get(sid) is session and not session.get("_closing")
                and not session.get("_finalized")
                and session.get("transport_owner") == transport_owner and session.get("profile_home") == profile_home
                and int(session.get("_queued_prompt_generation", 0)) == generation)

    def current():
        with session["history_lock"]:
            return current_locked()

    def own(lease):
        with session["history_lock"]:
            session.setdefault("_approval_task_leases", WeakSet()).add(lease)
            if not current_locked():
                lease.revoke()

    from hermes_constants import get_process_hermes_home
    return approval_task_failure_scope(approval_task_context(text, identity=requester,
        profile=str(session.get("profile_home") or get_process_hermes_home()),
        session=str(session.get("session_key") or sid), task_id="command:" + request_id,
        run_id=str(generation), is_current=current, owner=own))
