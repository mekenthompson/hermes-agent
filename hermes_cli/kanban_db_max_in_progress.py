"""Per-tick ``kanban.max_in_progress`` resolution that a bad edit can never loosen.

Shared by the gateway-embedded dispatcher and the standalone ``run_daemon`` loop
so both paths behave identically (HF-789). The memory-derived default lives in
``hermes_cli.kanban_db_dispatch`` (its test seams patch it there), so callers pass it in.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any, Optional

logger = logging.getLogger("hermes_cli.kanban_db_dispatch")  # share the dispatcher log channel

_NOT_WARNED = object()


def read_max_in_progress_setting() -> tuple[str, Optional[int], Any]:
    """Classify ``kanban.max_in_progress`` as ``(state, cap, raw)``.

    ``state`` is ``"absent"`` (not configured: use the memory-derived default),
    ``"valid"`` (a positive integer, returned as ``cap``) or ``"invalid"``
    (present but unusable: non-integer, < 1, empty, or the config could not be
    read). Per-tick callers keep their last good cap on ``"invalid"`` rather
    than loosening it; see :class:`MaxInProgressTracker`.
    """
    try:
        from hermes_cli.config import load_config_readonly
        kanban_cfg = (load_config_readonly() or {}).get("kanban", {})
    except Exception as exc:  # health: allow BLE001 -- any config failure keeps the last good cap, never "unlimited"
        return ("invalid", None, f"<config read error: {type(exc).__name__}>")
    if kanban_cfg is None:
        return ("absent", None, None)
    if not isinstance(kanban_cfg, Mapping):
        return ("invalid", None, kanban_cfg)
    raw = kanban_cfg.get("max_in_progress")
    if raw is None:
        # The schema default is null, so the merged config can't tell "unset"
        # from a bare ``max_in_progress:`` the operator left empty. The user
        # layer (no DEFAULT_CONFIG merge) can: an empty key is an edit gone wrong.
        if _user_layer_has_empty_max_in_progress():
            return ("invalid", None, None)
        return ("absent", None, None)
    if isinstance(raw, bool) or (isinstance(raw, str) and not raw.strip()):
        return ("invalid", None, raw)
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return ("invalid", None, raw)
    return ("valid", ival, raw) if ival >= 1 else ("invalid", None, raw)


def _user_layer_has_empty_max_in_progress() -> bool:
    try:
        from hermes_cli.config_effective import load_user_config_effective
        user_kanban = (load_user_config_effective() or {}).get("kanban")
    except Exception:  # health: allow BLE001 -- presence probe only; the merged read above already succeeded
        return False
    return isinstance(user_kanban, Mapping) and "max_in_progress" in user_kanban and user_kanban["max_in_progress"] is None


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.
    Per-tick loops use :class:`MaxInProgressTracker` instead, which keeps the
    last good cap when the value turns invalid.
    """
    state, cap, _raw = read_max_in_progress_setting()
    return cap if state == "valid" else None


class MaxInProgressTracker:
    """Effective per-tick cap, remembering the last good value.

    * valid positive int: used, and remembered as the last good cap;
    * key absent: the memory-derived default (remembered too). When memory is
      unknown the last good cap is kept rather than going unlimited;
    * present but invalid (``abc``, ``0``, negative, empty, unreadable config):
      the last good cap is kept. Only with no good cap yet (invalid at boot) is
      the derived default used. One warning per distinct bad value, not per tick.
    """

    def __init__(self, initial: Optional[int] = None) -> None:
        valid_initial = isinstance(initial, int) and not isinstance(initial, bool) and initial >= 1
        self.last_good: Optional[int] = initial if valid_initial else None
        self._warned_for: Any = _NOT_WARNED
        self._lock = threading.Lock()

    def resolve(self, derive_default: Callable[[], Optional[int]]) -> Optional[int]:
        state, cap, raw = read_max_in_progress_setting()
        with self._lock:
            if state == "valid":
                self._warned_for = _NOT_WARNED
                self.last_good = cap
                return cap
            if state == "absent":
                self._warned_for = _NOT_WARNED
                derived = derive_default()
                if derived is not None:
                    self.last_good = derived
                return self.last_good
            self._warn_once(raw)
            if self.last_good is None:
                self.last_good = derive_default()
            return self.last_good

    def _warn_once(self, raw: Any) -> None:
        marker = repr(raw)
        if marker == self._warned_for:
            return
        self._warned_for = marker
        if self.last_good is not None:
            logger.warning("kanban dispatcher: invalid kanban.max_in_progress=%r; keeping last good cap %d",
                           raw, self.last_good)
        else:
            logger.warning("kanban dispatcher: invalid kanban.max_in_progress=%r; using memory-derived default", raw)
