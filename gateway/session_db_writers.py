"""Stop same-home SQLite writers around gateway --replace.

Dashboard is an independent s6 service. `hermes gateway run --replace` historically
SIGTERM'd only the gateway PID. Dashboard kept state.db-wal/shm open, the exiting
gateway unlinked that generation, and the new gateway hit DeletedWalGenerationError.

Hold dashboard down first, then replace the gateway, then bring dashboard back only
after the new gateway process is the one that will open SQLite.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

_DASHBOARD_SERVICE_CANDIDATES = (
    Path("/run/service/dashboard"),
    Path("/run/s6-rc/servicedirs/dashboard"),
)


def dashboard_service_dirs() -> List[Path]:
    """Return existing s6 dashboard supervise dirs."""
    found: List[Path] = []
    for path in _DASHBOARD_SERVICE_CANDIDATES:
        supervise = path / "supervise"
        if path.is_dir() and (supervise.is_dir() or (path / "run").is_file()):
            found.append(path)
    return found


def _s6_svc(flag: str, service: Path) -> bool:
    try:
        result = subprocess.run(
            ["s6-svc", flag, str(service)],
            capture_output=True, timeout=5, check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("s6-svc %s %s failed: %s", flag, service, exc)
        return False
    if result.returncode != 0:
        logger.warning(
            "s6-svc %s %s exited %s: %s",
            flag, service, result.returncode,
            (result.stderr or result.stdout or b"").decode("utf-8", "replace")[:200],
        )
        return False
    return True


def hold_supervised_dashboard_down() -> List[Path]:
    """s6-svc -d so supervise will not restart dashboard during gateway replace."""
    held: List[Path] = []
    for service in dashboard_service_dirs():
        if _s6_svc("-d", service):
            held.append(service)
            logger.info("Held dashboard service down at %s for gateway replace", service)
    return held


def release_supervised_dashboard(held: Sequence[Path]) -> None:
    """s6-svc -u after the replacement gateway owns SQLite."""
    for service in held:
        if _s6_svc("-u", service):
            logger.info("Released dashboard service at %s after gateway replace", service)


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _environ_hermes_home(pid: int) -> Optional[str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for item in raw.split(b"\x00"):
        if item.startswith(b"HERMES_HOME="):
            return item.split(b"=", 1)[1].decode("utf-8", "replace")
    return None


def iter_same_home_dashboard_pids(hermes_home: Path, *, except_pids: Iterable[int] = ()) -> List[int]:
    """PIDs whose cmdline is hermes dashboard/serve and HERMES_HOME matches."""
    home = os.path.realpath(str(hermes_home))
    skip = {int(p) for p in except_pids}
    skip.add(os.getpid())
    found: List[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in skip:
            continue
        cmd = _cmdline(pid)
        if "hermes" not in cmd:
            continue
        if "dashboard" not in cmd and " serve" not in cmd and not cmd.rstrip().endswith("serve"):
            continue
        if "gateway" in cmd and "dashboard" not in cmd:
            continue
        env_home = _environ_hermes_home(pid)
        if env_home is None:
            continue
        if os.path.realpath(env_home) != home:
            continue
        found.append(pid)
    return found


def terminate_pids(pids: Sequence[int], *, force: bool = False) -> None:
    sig = getattr(signal, 'SIGKILL', signal.SIGTERM) if force else signal.SIGTERM  # windows-footgun: ok
    for pid in pids:
        try:
            os.kill(pid, sig)  # windows-footgun: ok — POSIX /proc dashboard PIDs only
        except ProcessLookupError:
            continue
        except PermissionError:
            logger.warning("Permission denied signalling dashboard PID %s", pid)


def wait_deleted_sidecar_holders_gone(db_path: Path, *, timeout_s: float = 10.0) -> bool:
    """True when no process holds a deleted WAL/SHM inode for db_path."""
    try:
        from hermes_state_dbfile import iter_deleted_sqlite_sidecar_holders
    except ImportError:
        logger.warning("iter_deleted_sqlite_sidecar_holders unavailable; skipping waiter")
        return True
    deadline = time.monotonic() + timeout_s
    while True:
        holders = list(iter_deleted_sqlite_sidecar_holders(db_path))
        if not holders:
            return True
        if time.monotonic() >= deadline:
            logger.error(
                "Deleted WAL/SHM holders still present for %s after %.1fs: %s",
                db_path, timeout_s, holders[:8],
            )
            return False
        time.sleep(0.1)


def quiesce_session_db_writers_for_replace(
    hermes_home: Path, *, except_pids: Iterable[int] = (),
) -> List[Path]:
    """Hold dashboard down, SIGTERM leftover dashboard PIDs, return s6 dirs to release later."""
    held = hold_supervised_dashboard_down()
    pids = iter_same_home_dashboard_pids(hermes_home, except_pids=except_pids)
    if pids:
        logger.info("SIGTERM dashboard/serve PIDs before gateway replace: %s", pids)
        terminate_pids(pids, force=False)
    return held
