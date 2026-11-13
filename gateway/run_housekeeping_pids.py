"""Gateway housekeeping chore that warns before a container's cgroup task ceiling stalls the agent.

``pids.max`` counts threads as well as processes. A gateway that also hosts a browser, MCP stdio
servers and subagents can hit it, and the symptom is a scatter of ``can't start new thread`` /
``EAGAIN`` failures rather than one clear error. One WARNING per episode near the ceiling (and one
whenever the kernel recorded a refused fork since the last tick) turns that into a line in
``errors.log``. Two small file reads per tick; a host without cgroup v2 pid accounting is a no-op.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PIDS_WARN_RATIO = 0.8
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_state = {"high": False, "max_events": 0}


def _read_int(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _max_events(path: Path) -> Optional[int]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, value = line.partition(" ")
        if key == "max" and value.strip().isdigit():
            return int(value.strip())
    return None


def check_cgroup_pids(cgroup_root: Path = _CGROUP_ROOT) -> None:
    """Warn when ``pids.current`` exceeds 80% of a finite ``pids.max`` or new limit refusals appear."""
    limit = _read_int(cgroup_root / "pids.max")  # "max" (unlimited) reads as None
    current = _read_int(cgroup_root / "pids.current")
    if limit is None or current is None or limit <= 0:
        return
    events = _max_events(cgroup_root / "pids.events")
    previous_events = _state["max_events"]
    if events is not None:
        _state["max_events"] = events
    if events is not None and events > previous_events:
        logger.warning(
            "cgroup pids limit hit: %d fork/thread start(s) refused since last check (pids.current=%d, "
            "pids.max=%d); expect \"can't start new thread\" errors", events - previous_events, current, limit)
    high = current > limit * PIDS_WARN_RATIO
    if high and not _state["high"]:
        logger.warning(
            "cgroup pids near limit: pids.current=%d of pids.max=%d (%.0f%%); new threads and processes "
            "will fail at the limit", current, limit, 100.0 * current / limit)
    elif not high and _state["high"]:
        logger.info("cgroup pids back under %.0f%%: pids.current=%d of pids.max=%d",
                    100 * PIDS_WARN_RATIO, current, limit)
    _state["high"] = high
