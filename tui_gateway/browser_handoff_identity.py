"""Project a verified Desktop gateway identity into a bounded handoff claim."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from hermes_cli.dashboard_auth.ws_tickets import INTERNAL_PROVIDER, INTERNAL_USER_ID

_EXCLUDED_IDENTITIES = frozenset({
    (INTERNAL_PROVIDER, INTERNAL_USER_ID),
    ("dashboard-session-token", "dashboard-session-token"),
})


@dataclass(frozen=True, slots=True)
class BrowserHandoffIdentity:
    """Non-secret provider/subject claim copied from a verified WebSocket identity."""

    provider: str
    subject: str


def browser_handoff_identity(identity: object) -> BrowserHandoffIdentity | None:
    """Return only a complete human ticket identity stamped by the server."""
    if not isinstance(identity, Mapping) or set(identity) != {"user_id", "provider"}:
        return None
    subject, provider = identity["user_id"], identity["provider"]
    if not isinstance(subject, str) or not isinstance(provider, str):
        return None
    if not subject.strip() or not provider.strip():
        return None
    if (provider, subject) in _EXCLUDED_IDENTITIES:
        return None
    return BrowserHandoffIdentity(provider=provider, subject=subject)
