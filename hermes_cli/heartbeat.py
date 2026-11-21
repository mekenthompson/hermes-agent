"""Session heartbeats — recurring re-entry prompts for the current session.

Session-scoped and in-process (CLI or gateway must be running); durable cross-process scheduling stays
``hermes cron``. Invariants (mirrors goals.py): injection is a plain user message — no system-prompt
mutation or toolset swap, so prompt caching stays intact — and a real user message always wins:
heartbeats only fire into an idle session with an empty input queue."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 60  # floor: re-entering more often than once a minute is a busy-loop, not a heartbeat
POLL_SECONDS = 5.0  # how often drivers poll for due heartbeats; not user-facing

HEARTBEAT_PROMPT_TEMPLATE = (
    "[Heartbeat — recurring instruction, fires every {interval}]\n{prompt}\n\n"
    "If there is nothing meaningful to do or report for this instruction "
    "right now, reply briefly that nothing has changed and stop — do not invent work."
)

_INTERVAL_RE = re.compile(
    r"^\s*(?:every\s+)?(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$", re.IGNORECASE)

_UNIT_SECONDS = {
    **dict.fromkeys(("s", "sec", "secs", "second", "seconds"), 1),
    **dict.fromkeys(("m", "min", "mins", "minute", "minutes"), 60),
    **dict.fromkeys(("h", "hr", "hrs", "hour", "hours"), 3600),
    **dict.fromkeys(("d", "day", "days"), 86400),
}

# field -> (coercer, default used when the stored value is missing/falsy)
USER_HEARTBEAT_ORIGIN = "user"
ACTIVE_WORK_HEARTBEAT_ORIGIN = "active-work"
# A managed active-work check is a cheap scan every ``interval_seconds`` that wakes the model only when
# the tracked work changed (``work_fingerprint`` != ``woken_fingerprint``). Completions and stops already
# re-enter the session as their own messages, so what remains is a silent hang: with work still running
# and nothing different, wake once per this floor. 30 minutes caps an unchanged long job at ~48 model
# turns a day (vs ~480 at the 180s cadence) while a stall, a stop or an exit is still seen at the next
# scan through the fingerprint, not through this floor.
ACTIVE_WORK_LIVENESS_FLOOR_SECONDS = 30 * 60

_STATE_FIELDS = {
    "prompt": (str, ""), "interval_seconds": (int, 0), "status": (str, "active"),
    "created_at": (float, 0.0), "last_fired_at": (float, 0.0), "fire_count": (int, 0),
    "origin": (str, USER_HEARTBEAT_ORIGIN),
    "work_fingerprint": (str, ""), "woken_fingerprint": (str, ""),
}


def parse_interval(text: str) -> Optional[int]:
    """Parse ``10m`` / ``every 2h`` / ``every 90 minutes`` into seconds.

    None when not an interval; below ``MIN_INTERVAL_SECONDS`` returns -1 so callers can tell "too small" apart.
    """
    m = _INTERVAL_RE.match(text) if text else None
    if not m:
        return None
    seconds = int(float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])
    return -1 if seconds < MIN_INTERVAL_SECONDS else seconds


def format_interval(seconds: int) -> str:
    """Human-readable interval (``600`` → ``10m``)."""
    seconds = int(seconds)
    units = ((86400, "d"), (3600, "h"), (60, "m"))
    return next((f"{seconds // unit}{suffix}" for unit, suffix in units if seconds % unit == 0), f"{seconds}s")


@dataclass
class HeartbeatState:
    """Serializable per-session heartbeat."""

    prompt: str
    interval_seconds: int
    status: str = "active"          # active | paused | cleared
    created_at: float = 0.0
    last_fired_at: float = 0.0
    fire_count: int = 0
    origin: str = USER_HEARTBEAT_ORIGIN  # user | active-work; missing legacy rows are user-authored
    # active-work only: the tracked work as the last scan saw it / as it was when the model was last woken.
    work_fingerprint: str = ""
    woken_fingerprint: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> HeartbeatState:
        data = json.loads(raw)
        return cls(**{name: coerce(data.get(name) or default) for name, (coerce, default) in _STATE_FIELDS.items()})

    def is_due(self, now: Optional[float] = None) -> bool:
        if self.status != "active" or not self.prompt or self.interval_seconds <= 0:
            return False
        return (time.time() if now is None else now) - (self.last_fired_at or self.created_at) >= self.interval_seconds

    def has_news(self, now: Optional[float] = None) -> bool:
        """Whether a due tick is worth a model turn. A user-authored heartbeat always is; a managed
        active-work check only when its tracked work changed since the last wake or the liveness floor
        has passed (``last_fired_at`` is the last wake: a tick with no news is never claimed)."""
        if self.origin != ACTIVE_WORK_HEARTBEAT_ORIGIN:
            return True
        now = time.time() if now is None else now
        return (self.work_fingerprint != self.woken_fingerprint
                or now - (self.last_fired_at or self.created_at) >= ACTIVE_WORK_LIVENESS_FLOOR_SECONDS)

    def render_prompt(self) -> str:
        interval = format_interval(self.interval_seconds)
        if self.origin == ACTIVE_WORK_HEARTBEAT_ORIGIN:
            # The generic footer asks for a visible "nothing has changed" reply.
            # This check must stay silent unless the marker is exact [SILENT].
            return (
                "[Heartbeat — recurring instruction, fires every "
                f"{interval}]\n{self.prompt}\n\n"
                "If nothing meaningful changed, reply exactly [SILENT]. "
                "Do not invent progress."
            )
        return HEARTBEAT_PROMPT_TEMPLATE.format(interval=interval, prompt=self.prompt)


def _get_session_db() -> Optional[Any]:
    """Persistence goes through the goals module's per-HERMES_HOME cached store handle (one shared handle)."""
    try:
        from hermes_cli.goals import _get_session_db as _goals_db
        return _goals_db()
    except Exception as exc:  # pragma: no cover
        logger.debug("HeartbeatManager: store bootstrap failed (%s)", exc)
        return None


_META_PREFIX = "heartbeat:"
_CLAIM_LOCK = threading.Lock()  # every driver of a session's heartbeat runs in its owning gateway process


def load_heartbeat(session_id: str) -> Optional[HeartbeatState]:
    db = _get_session_db() if session_id else None
    if db is None:
        return None
    try:
        raw = db.state_meta.get(_META_PREFIX + session_id)
    except Exception as exc:
        logger.debug("HeartbeatManager: get_meta failed: %s", exc)
        return None
    try:
        state = HeartbeatState.from_json(raw) if raw else None
    except Exception as exc:
        logger.warning("HeartbeatManager: could not parse stored heartbeat for %s: %s", session_id, exc)
        return None
    return None if state is None or state.status == "cleared" else state


def store_has_active_heartbeat(db: Any) -> bool:
    """True when *db* holds an ACTIVE ``heartbeat:*`` row — or one that cannot be parsed (unknown, so
    the caller keeps its full sweep). ``clear``/``pause`` keep their rows (status ``cleared``/``paused``),
    so key existence alone is not "active". Read errors propagate: "unavailable" is the caller's call."""
    for entry in db.state_meta.list_prefix(_META_PREFIX):
        try:
            if HeartbeatState.from_json(entry.value).status == "active":
                return True
        except Exception:
            return True
    return False


def save_heartbeat(session_id: str, state: HeartbeatState) -> None:
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        from hermes_cli.goals import _warn_dropped_write
        _warn_dropped_write("HeartbeatManager", "heartbeat", session_id)
        return
    try:
        db.state_meta.set(_META_PREFIX + session_id, state.to_json())
    except Exception as exc:
        logger.debug("HeartbeatManager: set_meta failed: %s", exc)


class HeartbeatManager:
    """Per-session heartbeat state + due-tick decisions; the surface CLI + gateway talk to.

    Drivers (CLI thread / gateway task) call :meth:`due_prompt` on a poll cadence while the session is
    idle; a non-None return is the user-role message to inject.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._state: Optional[HeartbeatState] = load_heartbeat(session_id)
        # (last_fired_at, fire_count, woken_fingerprint) before the last due_prompt
        self._last_claim: Optional[tuple[float, int, str]] = None

    @property
    def state(self) -> Optional[HeartbeatState]:
        return self._state

    def has_heartbeat(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def status_line(self) -> str:
        s = self._state
        if s is None:
            return "No heartbeat. Set one with /heartbeat every <interval> <prompt>."
        every = format_interval(s.interval_seconds)
        fired = f", fired {s.fire_count}×" if s.fire_count else ""
        if s.status == "active":
            next_in = max(0, int((s.last_fired_at or s.created_at) + s.interval_seconds - time.time()))
            return f"♥ Heartbeat (every {every}, next in ~{next_in}s{fired}): {s.prompt}"
        icon = "⏸ " if s.status == "paused" else ""
        return f"{icon}Heartbeat ({s.status}, every {every}{fired}): {s.prompt}"

    def set(self, prompt: str, interval_seconds: int) -> HeartbeatState:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("heartbeat prompt is empty")
        interval_seconds = int(interval_seconds)
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS}s")
        # A slash-command set is user-authored and replaces any managed check.
        self._state = HeartbeatState(prompt=prompt, interval_seconds=interval_seconds, status="active",
                                     created_at=time.time(), origin=USER_HEARTBEAT_ORIGIN)
        save_heartbeat(self.session_id, self._state)
        return self._state

    def _set_status(self, status: str, *, reanchor: bool = False) -> Optional[HeartbeatState]:
        if not self._state:
            return None
        with _CLAIM_LOCK:  # a stop landing inside a claim's reread-then-write would be overwritten by it
            self._state.status = status
            if reanchor:
                self._state.last_fired_at = time.time()
            save_heartbeat(self.session_id, self._state)
        return self._state

    def pause(self) -> Optional[HeartbeatState]:
        return self._set_status("paused")

    def resume(self) -> Optional[HeartbeatState]:
        # Re-anchor so resuming doesn't instantly fire a stale tick.
        return self._set_status("active", reanchor=True)

    def clear(self) -> bool:
        cleared = self._set_status("cleared") is not None
        self._state = None
        return cleared

    def reload(self) -> Optional[HeartbeatState]:
        """Refresh from SessionDB so a sibling writer is visible."""
        self._state = load_heartbeat(self.session_id)
        return self._state

    def ensure_managed(self, prompt: str, interval_seconds: int, *, origin: str, work_fingerprint: str = "") -> str:
        """Install or refresh a managed heartbeat. Never replace a different origin.

        ``work_fingerprint`` is the tracked work as the caller just scanned it. A new check (and a row
        saved before fingerprints existed) takes it as already-woken, so the work the model started
        this turn does not earn its own wake; later changes do.

        Returns ``installed``, ``kept``, ``updated``, or ``preserved-user``.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("heartbeat prompt is empty")
        interval_seconds = int(interval_seconds)
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS}s")
        if origin == USER_HEARTBEAT_ORIGIN:
            raise ValueError("managed heartbeats cannot use the user origin")
        with _CLAIM_LOCK:  # the fingerprint write must not lose a claim (or be lost to one) landing in between
            current = self.reload()
            if current is not None and current.status in {"active", "paused"} and current.origin != origin:
                return "preserved-user"
            if current is not None and current.origin == origin and current.status == "active":
                action = "kept" if (current.prompt, current.interval_seconds) == (prompt, interval_seconds) else "updated"
                refreshed = (prompt, interval_seconds, work_fingerprint, current.woken_fingerprint or work_fingerprint)
                if refreshed != (current.prompt, current.interval_seconds, current.work_fingerprint, current.woken_fingerprint):
                    (current.prompt, current.interval_seconds,
                     current.work_fingerprint, current.woken_fingerprint) = refreshed
                    save_heartbeat(self.session_id, current)
                return action
            created_at = current.created_at if current and current.origin == origin else time.time()
            last_fired = current.last_fired_at if current and current.origin == origin else 0.0
            fire_count = current.fire_count if current and current.origin == origin else 0
            self._state = HeartbeatState(
                prompt=prompt, interval_seconds=interval_seconds, status="active",
                created_at=created_at, last_fired_at=last_fired, fire_count=fire_count, origin=origin,
                work_fingerprint=work_fingerprint, woken_fingerprint=work_fingerprint,
            )
            save_heartbeat(self.session_id, self._state)
            return "installed"

    def clear_if_origin(self, origin: str) -> bool:
        """Clear only a heartbeat this origin owns. User-authored rows stay."""
        current = self.reload()
        if current is None or current.origin != origin:
            return False
        return self.clear()

    def due_prompt(self, now: Optional[float] = None) -> Optional[str]:
        """Return the injection prompt if the heartbeat is due, else None.

        The fire is recorded immediately (before the turn runs) so overlapping polls or a long turn can never
        double-fire the same tick. Missed ticks coalesce: the anchor resets to NOW, not the theoretical
        schedule.
        """
        with _CLAIM_LOCK:
            s = self._state
            if s is None or not s.is_due(now) or not s.has_news(now):
                return None
            # Compare-and-set on the stored row: the gateway poller and a viewer's due_prompt RPC each hold
            # their own manager over it, and only one may take the tick. A row cleared or paused since is lost too.
            stored = load_heartbeat(self.session_id)
            if stored is None or stored.status != "active" or (
                    stored.last_fired_at, stored.fire_count) != (s.last_fired_at, s.fire_count):
                self._state = stored
                return None
            self._last_claim = (s.last_fired_at, s.fire_count, s.woken_fingerprint)
            s.last_fired_at = now if now is not None else time.time()
            s.fire_count += 1
            s.woken_fingerprint = s.work_fingerprint
            save_heartbeat(self.session_id, s)
            return s.render_prompt()

    def abandon_fire(self) -> bool:
        """Rewind the fire recorded by the last :meth:`due_prompt` whose turn never started, so the tick stays
        due for the next poll instead of being silently consumed. Mirrors ``LoopManager.abandon_tick``. Skipped
        (False) when the persisted state moved on — a pause/resume/clear that landed in between wins."""
        claim, s = self._last_claim, self._state
        if claim is None or s is None:
            return False
        current = load_heartbeat(self.session_id)
        if current is None or current.status != "active" or (current.last_fired_at, current.fire_count) != (
                s.last_fired_at, s.fire_count):
            return False
        s.last_fired_at, s.fire_count, s.woken_fingerprint = claim
        self._last_claim = None
        save_heartbeat(self.session_id, s)
        return True


def migrate_heartbeat_to_session(old_session_id: str, new_session_id: str) -> bool:
    """Carry a heartbeat across a compression session rotation (copy to child, archive parent, never raise).

    Same shape as ``goals.migrate_goal_to_session``.
    """
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        state = load_heartbeat(old_session_id)
        if state is None or load_heartbeat(new_session_id) is not None:
            return False
        save_heartbeat(new_session_id, state)
        state.status = "cleared"
        save_heartbeat(old_session_id, state)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("HeartbeatManager: migration failed: %s", exc)
        return False


__all__ = [
    "HEARTBEAT_PROMPT_TEMPLATE",
    "MIN_INTERVAL_SECONDS",
    "POLL_SECONDS",
    "HeartbeatManager",
    "HeartbeatState",
    "format_interval",
    "load_heartbeat",
    "migrate_heartbeat_to_session",
    "parse_interval",
    "save_heartbeat",
]
