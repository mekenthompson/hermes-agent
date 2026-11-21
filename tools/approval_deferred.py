"""Deferred gateway approvals: an unanswered prompt parks instead of ending the line of work.

A gateway approval that times out used to resolve as a hard BLOCKED ("do not retry, do not rephrase"),
so a user who was away when the card arrived killed that line of work for good, and their later tap
on the still-visible card resolved nothing. With ``approvals.deferred_ttl`` (default 6h; ``0`` restores
the old behaviour) the timeout PARKS the entry here instead:

* the agent gets a DEFERRED result: nothing ran and nothing was consented to, it must not rephrase
  around the request, and it may carry on with unrelated work;
* the card stays answerable through the ordinary answer paths (``/approve``, a card tap, Desktop):
  ``tools.approval`` resolves a parked entry by ``request_id`` exactly like a live one;
* a late answer is recorded as ONE decision bound to the exact command, and announced to the
  originating session as an ``approval_decision`` notification.

Security shape (every point has a test in ``tests/tools/test_approval_deferred.py``):

* A late answer NEVER runs anything. It is spent only when the agent re-issues the byte-identical
  command, which first passes every pre-gate floor (hardline, user deny rules, allowlist, scan budget,
  stale-task revocation) and only then reaches the human step, where the recorded answer stands in for
  the prompt. A floor that tightened since the answer still blocks.
* ``once`` covers one run. ``session`` / ``always`` persist at that re-issue, in the re-issuing turn's own
  profile scope, through the same ``grant`` path as a live answer; an entry that offered only a single
  operation (smart-DENY override, scan-incomplete, manual-only) is downgraded to ``once`` as it was live.
  ``deny`` blocks one re-issue and persists nothing.
* A decision is bound to (session, profile scope, exact command, pattern keys, requester/approvers),
  consumed once, and dropped at ``approvals.deferred_ttl`` or when the session is cleared. A reworded
  command matches nothing and prompts afresh; an identical command re-issued while its card is still
  pending answers DEFERRED again without a second card.
* Parked entries are not "blocking": they never count as a busy backend and a bare conversational
  "yes" never answers them; only an explicit ``/approve``, ``/deny`` or card tap with the request id does.

State here is guarded by ``tools.approval._lock`` (read through that module at call time).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from tools import approval_context as _ctx

logger = logging.getLogger("tools.approval")

# Per session: a runaway agent must not turn the registry into an unbounded list of open cards.
_MAX_PARKED_PER_SESSION = 8
_MAX_DECISIONS = 128

_parked: dict[str, list] = {}  # session_key -> parked _ApprovalEntry, oldest first
_decisions: dict[tuple, LateDecision] = {}  # (grant scope, identity digest, pattern keys) -> answer

# Session vars the originating chat is routed by, as ``terminal_tool_background._ROUTING_FIELDS``
# stamps them on a background process: the answer is delivered the way a completion notice is.
_ROUTE_ENV = (
    ("platform", "HERMES_SESSION_PLATFORM"), ("chat_id", "HERMES_SESSION_CHAT_ID"),
    ("user_id", "HERMES_SESSION_USER_ID"), ("user_name", "HERMES_SESSION_USER_NAME"),
    ("thread_id", "HERMES_SESSION_THREAD_ID"), ("message_id", "HERMES_SESSION_MESSAGE_ID"),
    ("parent_session_id", "HERMES_SESSION_ID"),
)
# The notice carries the redacted display command; bound it so one huge heredoc cannot flood a chat.
_NOTICE_COMMAND_CHARS = 1500


@dataclass(frozen=True)
class LateDecision:
    """A user's answer to a parked approval, waiting for the agent to re-issue the same command."""
    choice: str  # once | session | always | deny
    reason: Optional[str]
    authority: dict
    one_shot: bool  # the card offered no session/always scope: any such answer is one operation
    expires_at: float
    request_id: str


def _lock():
    from tools import approval as _approval
    return _approval._lock


def _decision_key(session_key: str, identity: str, pattern_keys) -> tuple:
    from tools import approval as _approval
    return (_approval._session_grant_key(session_key), identity, tuple(pattern_keys or ()))


def park(session_key: str, entry) -> tuple[bool, list]:
    """Keep a timed-out *entry* answerable. Caller holds the approval lock and has already removed the
    entry from the live queue. Returns ``(parked, evicted)``: ``parked`` is False when deferral is off;
    ``evicted`` are older parked entries pushed out by the per-session cap, whose settle hooks the
    caller runs (``run_settle``) once it has released the lock."""
    ttl = _ctx._get_approval_deferred_ttl()
    if ttl <= 0:
        return False, []
    from tools import approval as _approval
    entry.deferred = True
    # Captured here, in the waiting agent thread: the answer arrives on a gateway thread whose
    # profile scope and session vars are not the requester's.
    entry.grant_scope = _approval._session_grant_key(session_key)
    entry.route = {key: _ctx._session_env(env) for key, env in _ROUTE_ENV}
    entry.expires_at = time.monotonic() + ttl
    parked = _parked.setdefault(session_key, [])
    parked.append(entry)
    evicted = parked[:-_MAX_PARKED_PER_SESSION]
    del parked[:-_MAX_PARKED_PER_SESSION]
    return True, evicted


def run_settle(entries, reason: str) -> None:
    """Run each entry's surface hooks (card retirement, Desktop ``request.cancel``) once. Callers hold no lock."""
    for entry in entries:
        settle, entry.settle = entry.settle, None
        observers, entry.observers = entry.observers, []
        for callback in ([settle] if settle is not None else []) + observers:
            try:
                callback(reason)
            except Exception:
                logger.debug("deferred approval settle hook failed", exc_info=True)


def _live(session_key: str) -> list:
    """Parked entries of the session that have not outlived the TTL. Caller holds the lock."""
    now = time.monotonic()
    return [e for e in _parked.get(session_key, ()) if e.expires_at > now]


def expire_due() -> None:
    """Drop parked entries and recorded answers past their TTL; their cards are settled as timed out."""
    now = time.monotonic()
    dead = []
    with _lock():
        for session_key in list(_parked):
            keep = [e for e in _parked[session_key] if e.expires_at > now]
            dead += [e for e in _parked[session_key] if e.expires_at <= now]
            if keep:
                _parked[session_key] = keep
            else:
                del _parked[session_key]
        for key in [k for k, d in _decisions.items() if d.expires_at <= now]:
            del _decisions[key]
    run_settle(dead, "timeout")


def entry_for_request(session_key: str, request_id: Optional[str]):
    """The live parked entry with this ``request_id``, else None. Caller holds the lock."""
    if not request_id:
        return None
    return next((e for e in _live(session_key) if e.data.get("request_id") == request_id), None)


def has_parked(session_key: str) -> bool:
    with _lock():
        return bool(_live(session_key))


def snapshots(session_key: str) -> list[dict]:
    with _lock():
        return [dict(e.data) for e in _live(session_key)]


def authority_rows(session_key: str) -> list[dict]:
    with _lock():
        return [{"request_id": e.data.get("request_id"), **e.authority} for e in _live(session_key)]


def pending_match(session_key: str, command: str, pattern_keys):
    """The parked entry this command is a re-issue of, else None. The same identity (digest of what
    executes), pattern keys, profile scope and answerers as the parked request: a different requester's
    identical command is its own request."""
    from tools.approval_gateway_wait import _identity_digest, _same_answerers
    key = _decision_key(session_key, _identity_digest(command), pattern_keys)
    authority = _ctx.prompt_authority()
    with _lock():
        return next((e for e in _live(session_key)
                     if (e.grant_scope, e.identity, tuple(e.data.get("pattern_keys") or ())) == key
                     and _same_answerers(e.authority, authority)), None)


def take_decision(session_key: str, command: str, pattern_keys) -> Optional[LateDecision]:
    """Spend the recorded late answer for this exact command, once. None when there is none, it expired,
    or it was given for other answerers (the command then prompts afresh like any new request)."""
    from tools.approval_gateway_wait import _identity_digest, _same_answerers
    key = _decision_key(session_key, _identity_digest(command), pattern_keys)
    authority = _ctx.prompt_authority()
    with _lock():
        decision = _decisions.get(key)
        if decision is None:
            return None
        if decision.expires_at <= time.monotonic():
            del _decisions[key]
            return None
        if not _same_answerers(decision.authority, authority):
            return None
        del _decisions[key]
        return decision


def commit(session_key: str, request_id: str, choice: str, reason: Optional[str]):
    """Record a late answer for a parked request and unpark it; the parked entry, or None when nothing
    was parked under that id or the answer is not a recognised decision (the card then stays parked, so a
    garbled answer can neither consent nor discard the request). Caller holds the lock."""
    from tools import approval as _approval
    entry = entry_for_request(session_key, request_id)
    if entry is None:
        return None
    if choice != "deny" and choice not in _approval.CONSENT_ANSWERS:
        logger.warning("approval: ignoring unrecognized late answer %r for deferred request %s", choice, request_id)
        return None
    _parked[session_key].remove(entry)
    if not _parked[session_key]:
        del _parked[session_key]
    key = (entry.grant_scope, entry.identity, tuple(entry.data.get("pattern_keys") or ()))
    _decisions.pop(key, None)
    _decisions[key] = LateDecision(
        choice=choice, reason=reason, authority=entry.authority,
        one_shot=not entry.data.get("allow_session", False),
        expires_at=time.monotonic() + _ctx._get_approval_deferred_ttl(), request_id=request_id)
    while len(_decisions) > _MAX_DECISIONS:
        del _decisions[next(iter(_decisions))]
    return entry


def clear_session(session_key: str) -> list:
    """Forget everything parked or decided for a session; returns its parked entries for ``run_settle``.
    Caller holds the lock."""
    for key in [k for k in _decisions if k[0] == session_key or (isinstance(k[0], tuple) and k[0][0] == session_key)]:
        del _decisions[key]
    return _parked.pop(session_key, [])


def announce(session_key: str, entry, choice: str, reason: Optional[str]) -> dict:
    """Tell the originating session its parked request was answered. The event rides the process
    registry's completion queue, the channel every background notice reaches a session by, so each
    surface's existing consumer (gateway injection, TUI/Desktop drain) delivers it. It names the
    decision only: nothing is executed here."""
    from tools.process_registry import process_registry
    request_id = entry.data["request_id"]
    event = {
        "type": "approval_decision", "event_id": uuid.uuid4().hex, "session_id": f"approval:{request_id}",
        "session_key": session_key, "request_id": request_id, "choice": choice, "reason": reason or "",
        "command": str(entry.data.get("command") or "")[:_NOTICE_COMMAND_CHARS],
        "description": str(entry.data.get("description") or ""),
        **entry.route,
    }
    process_registry.completion_queue.put(event)
    logger.info("Deferred approval answered late (request_id=%s, session=%s, choice=%s)", request_id, session_key, choice)
    return event
