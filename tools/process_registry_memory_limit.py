"""Per-worker cgroup memory ceiling for scope-wrapped background processes (split from process_registry)."""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tools.process_registry")

_MIN_WORKER_MEMORY_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_WORKER_MEMORY_MAX_BYTES = 1024 * 1024 * 1024
_WORKER_MEMORY_MAX_CAP_BYTES = 4 * 1024 * 1024 * 1024


def _worker_memory_max_bytes() -> int:
    """Finite per-worker cgroup limit that can never widen host risk.
    ``TERMINAL_LOCAL_MEMORY_MAX_MB`` is honored only when it *tightens* the safe
    bound (min of the gateway's cgroup-v2 ``memory.max`` and half of physical RAM,
    capped at 4 GiB), so an oversized override cannot exceed the enclosing slice.

    The proposed local-memory-guard environment override is honored when it tightens the safe bound, so this
    isolation composes with PR #57121 instead of inventing a second knob.
    """
    override_bound: Optional[int] = None
    override = os.getenv("TERMINAL_LOCAL_MEMORY_MAX_MB", "").strip()
    if override:
        try:
            parsed = int(override) * 1024 * 1024
        except ValueError:
            parsed = -1
        if parsed >= _MIN_WORKER_MEMORY_MAX_BYTES:
            override_bound = parsed
        else:
            logger.warning(
                "Ignoring invalid TERMINAL_LOCAL_MEMORY_MAX_MB=%r; "
                "expected an integer representing at least %d MiB",
                override, _MIN_WORKER_MEMORY_MAX_BYTES // (1024 * 1024))
    candidates: list[int] = []
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            if line.startswith("0::"):
                relative = line.partition("::")[2].lstrip("/")
                raw_limit = (
                    Path("/sys/fs/cgroup") / relative / "memory.max"
                ).read_text(encoding="utf-8-sig").strip()
                if raw_limit.isdigit():
                    cgroup_limit = int(raw_limit)
                    if cgroup_limit >= _MIN_WORKER_MEMORY_MAX_BYTES:
                        candidates.append(cgroup_limit)
                break
    except (OSError, ValueError):
        pass

    try:
        physical_bytes = int(os.sysconf("SC_PHYS_PAGES")) * int(
            os.sysconf("SC_PAGE_SIZE")
        )
        physical_bound = min(
            _WORKER_MEMORY_MAX_CAP_BYTES,
            max(_MIN_WORKER_MEMORY_MAX_BYTES, physical_bytes // 2),
        )
        candidates.append(physical_bound)
    except (OSError, ValueError, TypeError):
        pass
    safe_bound = min(candidates) if candidates else _DEFAULT_WORKER_MEMORY_MAX_BYTES
    return min(override_bound, safe_bound) if override_bound else safe_bound
