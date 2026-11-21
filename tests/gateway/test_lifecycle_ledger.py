"""Tests for gateway.lifecycle_ledger — unclean-shutdown detection (NS-608).

The ledger is a tiny sentinel state machine:
``record_startup`` claims ``state/gateway.lifecycle.json`` as
``phase=running``; every exit path calls ``mark_exited``; the next boot's
``record_startup``/``detect_unclean_exit`` reports a still-``running``
sentinel from a dead process as an unclean death (SIGKILL / OOM / VM loss)
and enriches the report with the last heartbeat's memory sample.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from gateway.lifecycle_ledger import (
    detect_unclean_exit,
    get_lifecycle_sentinel_path,
    mark_exited,
    read_prior_exit_label,
    record_startup,
    sample_memory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEAD_PID = 2 ** 22 + 12345  # beyond default pid_max on Linux; never alive


def _write_sentinel(home: Path, payload: dict) -> Path:
    path = get_lifecycle_sentinel_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read_sentinel(home: Path) -> dict:
    return json.loads(get_lifecycle_sentinel_path(home).read_text(encoding="utf-8"))


def _write_heartbeat(home: Path, payload: dict) -> Path:
    path = home / "state" / "gateway.heartbeat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _exit_diag_records(home: Path) -> list[dict]:
    path = home / "logs" / "gateway-exit-diag.log"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# sample_memory
# ---------------------------------------------------------------------------


@pytest.mark.platforms("linux")  # /proc is Linux-only
def test_sample_memory_has_expected_keys_on_linux() -> None:
    sample = sample_memory()
    assert sample.get("rss_kib", 0) > 0
    assert sample.get("mem_total_kib", 0) > 0
    assert "mem_available_kib" in sample


# --- container (cgroup) awareness -----------------------------------------

_GIB_KIB = 1024 * 1024
_V1_UNLIMITED = "9223372036854771712"


def _fake_meminfo(tmp_path: Path, total_kib: int = 62 * _GIB_KIB, avail_kib: int = 35 * _GIB_KIB) -> Path:
    path = tmp_path / "meminfo"
    path.write_text(
        f"MemTotal:       {total_kib} kB\nMemFree:         100 kB\n"
        f"MemAvailable:   {avail_kib} kB\nSwapTotal:      1000 kB\nSwapFree:        400 kB\n"
    )
    return path


def _patch_sources(monkeypatch, meminfo: Path, cgroup_root: Path) -> None:
    from gateway import lifecycle_ledger

    monkeypatch.setattr(lifecycle_ledger, "_MEMINFO_PATH", str(meminfo))
    monkeypatch.setattr(lifecycle_ledger, "_CGROUP_ROOT", cgroup_root)


def _cgroup_v2(root: Path, *, max_: str, current: int, inactive_file: int = 0, anon: int = 0) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.max").write_text(max_ + "\n")
    (root / "memory.current").write_text(f"{current}\n")
    (root / "memory.stat").write_text(f"anon {anon}\nfile 0\ninactive_file {inactive_file}\n")


def _cgroup_v1(root: Path, *, limit: str, usage: int, inactive_file: int = 0) -> None:
    mem = root / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "memory.limit_in_bytes").write_text(limit + "\n")
    (mem / "memory.usage_in_bytes").write_text(f"{usage}\n")
    (mem / "memory.stat").write_text(f"cache 0\ntotal_inactive_file {inactive_file}\n")


@pytest.mark.platforms("linux")
def test_sample_memory_v2_limit_caps_total_and_available(tmp_path, monkeypatch) -> None:
    cg = tmp_path / "cg"
    _cgroup_v2(cg, max_=str(12 * 1024**3), current=int(11.5 * 1024**3))
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), cg)
    sample = sample_memory()
    assert sample["mem_total_kib"] == 12 * _GIB_KIB
    assert sample["mem_available_kib"] == (12 * 1024**3 - int(11.5 * 1024**3)) // 1024
    assert sample["memory_source"] == "cgroup"
    assert sample["swap_used_kib"] == 600


@pytest.mark.platforms("linux")
def test_sample_memory_v2_available_excludes_reclaimable_inactive_file(tmp_path, monkeypatch) -> None:
    cg = tmp_path / "cg"
    _cgroup_v2(cg, max_=str(12 * 1024**3), current=11 * 1024**3, inactive_file=4 * 1024**3)
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), cg)
    sample = sample_memory()
    # usage without reclaimable cache = 7 GiB -> 5 GiB headroom
    assert sample["mem_available_kib"] == 5 * _GIB_KIB


@pytest.mark.platforms("linux")
def test_sample_memory_v2_max_is_unlimited_uses_meminfo(tmp_path, monkeypatch) -> None:
    cg = tmp_path / "cg"
    _cgroup_v2(cg, max_="max", current=11 * 1024**3)
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), cg)
    sample = sample_memory()
    assert sample["mem_total_kib"] == 62 * _GIB_KIB
    assert sample["mem_available_kib"] == 35 * _GIB_KIB
    assert sample["memory_source"] == "meminfo"


@pytest.mark.platforms("linux")
def test_sample_memory_v1_limit_caps_total_and_available(tmp_path, monkeypatch) -> None:
    cg = tmp_path / "cg"
    _cgroup_v1(cg, limit=str(8 * 1024**3), usage=7 * 1024**3, inactive_file=2 * 1024**3)
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), cg)
    sample = sample_memory()
    assert sample["mem_total_kib"] == 8 * _GIB_KIB
    assert sample["mem_available_kib"] == 3 * _GIB_KIB
    assert sample["memory_source"] == "cgroup"


@pytest.mark.platforms("linux")
def test_sample_memory_v1_sentinel_is_unlimited_uses_meminfo(tmp_path, monkeypatch) -> None:
    cg = tmp_path / "cg"
    _cgroup_v1(cg, limit=_V1_UNLIMITED, usage=7 * 1024**3)
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), cg)
    sample = sample_memory()
    assert sample["mem_total_kib"] == 62 * _GIB_KIB
    assert sample["mem_available_kib"] == 35 * _GIB_KIB
    assert sample["memory_source"] == "meminfo"


@pytest.mark.platforms("linux")
def test_sample_memory_unreadable_cgroup_falls_back_to_meminfo(tmp_path, monkeypatch) -> None:
    cg = tmp_path / "cg"
    cg.mkdir()
    (cg / "memory.max").write_text("garbage\n")
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), cg)
    sample = sample_memory()
    assert sample["mem_total_kib"] == 62 * _GIB_KIB
    assert sample["mem_available_kib"] == 35 * _GIB_KIB
    assert sample["memory_source"] == "meminfo"
    # no cgroup dir at all
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), tmp_path / "missing")
    assert sample_memory()["mem_total_kib"] == 62 * _GIB_KIB


@pytest.mark.platforms("linux")
def test_sample_memory_limit_above_host_never_inflates(tmp_path, monkeypatch) -> None:
    cg = tmp_path / "cg"
    _cgroup_v2(cg, max_=str(200 * 1024**3), current=1024**3)
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), cg)
    sample = sample_memory()
    assert sample["mem_total_kib"] == 62 * _GIB_KIB
    assert sample["mem_available_kib"] == 35 * _GIB_KIB


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("current", [None, "garbage"])
def test_sample_memory_unreadable_usage_clamps_available_to_limit(tmp_path, monkeypatch, current) -> None:
    # 4 GiB limit, host says 35 GiB free: without usage the host figure must
    # not leak through as more free memory than the container has.
    cg = tmp_path / "cg"
    _cgroup_v2(cg, max_=str(4 * 1024**3), current=0)
    if current is None:
        (cg / "memory.current").unlink()
    else:
        (cg / "memory.current").write_text(current + "\n")
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), cg)
    sample = sample_memory()
    assert sample["mem_total_kib"] == 4 * _GIB_KIB
    assert sample["mem_available_kib"] == 4 * _GIB_KIB
    assert sample["memory_source"] == "cgroup"


@pytest.mark.platforms("linux")
def test_sample_memory_unreadable_usage_keeps_smaller_host_available(tmp_path, monkeypatch) -> None:
    cg = tmp_path / "cg"
    _cgroup_v2(cg, max_=str(40 * 1024**3), current=0)
    (cg / "memory.current").unlink()
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path, avail_kib=3 * _GIB_KIB), cg)
    sample = sample_memory()
    assert sample["mem_total_kib"] == 40 * _GIB_KIB
    assert sample["mem_available_kib"] == 3 * _GIB_KIB


@pytest.mark.platforms("linux")
def test_sample_memory_cgroup_limit_drives_default_concurrency(tmp_path, monkeypatch) -> None:
    from hermes_cli.kanban_db_dispatch import derive_default_max_in_progress

    cg = tmp_path / "cg"
    _cgroup_v2(cg, max_=str(2 * 1024**3), current=1024**3)
    _patch_sources(monkeypatch, _fake_meminfo(tmp_path), cg)
    assert derive_default_max_in_progress(sample_memory()) == 4  # 2048 MiB / 512


# ---------------------------------------------------------------------------
# First boot / clean lifecycle
# ---------------------------------------------------------------------------


def test_first_boot_reports_nothing_and_claims_sentinel(tmp_path: Path) -> None:
    assert record_startup(home=tmp_path) is None
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] == os.getpid()
    assert "start_time" in sentinel


def test_clean_exit_then_boot_reports_nothing(tmp_path: Path) -> None:
    record_startup(home=tmp_path)
    mark_exited(0, reason="graceful_shutdown", home=tmp_path)

    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "exited"
    assert sentinel["exit_code"] == 0
    assert sentinel["exit_reason"] == "graceful_shutdown"

    assert record_startup(home=tmp_path) is None
    assert _exit_diag_records(tmp_path) == []


# ---------------------------------------------------------------------------
# Unclean-death detection
# ---------------------------------------------------------------------------


def test_running_sentinel_from_dead_pid_is_unclean(tmp_path: Path) -> None:
    _write_sentinel(tmp_path, {
        "phase": "running",
        "pid": _DEAD_PID,
        "start_time": 1000.0,
        "started_at": "2026-07-11T04:30:00+00:00",
    })

    evidence = detect_unclean_exit(home=tmp_path)
    assert evidence is not None
    assert evidence["prior_pid"] == _DEAD_PID
    assert evidence["prior_started_at"] == "2026-07-11T04:30:00+00:00"


def test_record_startup_persists_unclean_report_and_reclaims(tmp_path: Path) -> None:
    _write_sentinel(tmp_path, {
        "phase": "running",
        "pid": _DEAD_PID,
        "start_time": 1000.0,
        "started_at": "2026-07-11T04:30:00+00:00",
    })

    evidence = record_startup(home=tmp_path)
    assert evidence is not None

    records = _exit_diag_records(tmp_path)
    assert len(records) == 1
    assert records[0]["tag"] == "gateway.previous_unclean_exit"
    assert records[0]["prior_pid"] == _DEAD_PID
    assert records[0]["pid"] == os.getpid()

    # Sentinel reclaimed for the new life.
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] == os.getpid()




def test_record_startup_carries_unclean_flags_onto_new_sentinel(
    tmp_path: Path,
) -> None:
    """The unclean-death verdict must survive on the reclaimed sentinel so
    /api/status can surface "restarted after (suspected) OOM" (NS-656)."""
    _write_sentinel(tmp_path, {
        "phase": "running",
        "pid": _DEAD_PID,
        "start_time": 1000.0,
        "started_at": "2026-07-11T04:30:00+00:00",
    })
    # Last heartbeat shows near-exhausted memory → suspected OOM.
    from gateway.shutdown_watchdog import get_loop_heartbeat_path

    hb_path = get_loop_heartbeat_path(tmp_path)
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    hb_path.write_text(json.dumps({
        "pid": _DEAD_PID,
        "updated_at": "2026-07-11T05:00:00+00:00",
        "mem": {"mem_total_kib": 1024 * 1024, "mem_available_kib": 20 * 1024},
    }), encoding="utf-8")

    evidence = record_startup(home=tmp_path)
    assert evidence is not None
    assert evidence.get("suspected_oom") is True

    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["prior_unclean_exit"] is True
    assert sentinel["prior_suspected_oom"] is True


def test_record_startup_clean_boot_has_no_prior_flags(tmp_path: Path) -> None:
    _write_sentinel(tmp_path, {
        "phase": "exited",
        "pid": _DEAD_PID,
        "exit_code": 0,
        "exit_reason": "graceful_shutdown",
    })
    assert record_startup(home=tmp_path) is None
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert "prior_unclean_exit" not in sentinel
    assert "prior_suspected_oom" not in sentinel


# ---------------------------------------------------------------------------
# Takeover ownership guard on mark_exited
# ---------------------------------------------------------------------------


def test_mark_exited_leaves_pid_none_sentinel_alone(tmp_path: Path) -> None:
    """A sentinel with pid=None has unknown ownership — mark_exited must not
    clobber it with a clean-exit claim it cannot prove is its own."""
    _write_sentinel(tmp_path, {"phase": "running", "pid": None, "start_time": 2000.0})
    mark_exited(0, reason="graceful_shutdown", home=tmp_path)
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] is None


# ---------------------------------------------------------------------------
# read_prior_exit_label (container-boot annotation)
# ---------------------------------------------------------------------------


def test_prior_exit_label_survives_corrupt_sentinel(tmp_path: Path) -> None:
    path = get_lifecycle_sentinel_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage", encoding="utf-8")
    assert read_prior_exit_label(tmp_path) == "unknown"


def test_sentinel_carries_process_birth_through_exit(tmp_path: Path, monkeypatch) -> None:
    """The running sentinel stamps the process ``create_time`` (psutil birth, not the later ledger
    claim) and the exited sentinel keeps it, so the Windows start attestation can match a clean
    exit by incarnation, not by reusable PID (#110020)."""
    monkeypatch.setattr("hermes_cli.process_identity._process_create_time", lambda pid=None: 1234.5)
    record_startup(home=tmp_path)
    running = json.loads(get_lifecycle_sentinel_path(tmp_path).read_text(encoding="utf-8"))
    assert running["create_time"] == 1234.5
    mark_exited(0, reason="graceful_shutdown", home=tmp_path)
    exited = json.loads(get_lifecycle_sentinel_path(tmp_path).read_text(encoding="utf-8"))
    assert exited["phase"] == "exited"
    assert (exited["start_time"], exited["create_time"]) == (running["start_time"], 1234.5)


def test_replace_handover_is_not_a_death_and_pid_reuse_is(tmp_path: Path, monkeypatch) -> None:
    """The live-owner guard compares the sentinel's psutil ``create_time`` with the live PID's
    (same producer, epoch seconds). The ledger's ``start_time`` (claim time) is never compared
    with ``get_process_start_time`` (proc ticks / centiseconds): that comparison could not match,
    so a ``--replace`` handover was reported as an unclean death."""
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    monkeypatch.setattr("hermes_cli.process_identity._process_create_time", lambda pid=None: 5000.0)
    live = {"phase": "running", "pid": 4242, "start_time": 5003.7, "started_at": "x"}

    _write_sentinel(tmp_path, {**live, "create_time": 5000.0})
    assert detect_unclean_exit(home=tmp_path) is None  # same incarnation still alive: handover

    _write_sentinel(tmp_path, {**live, "create_time": 4000.0})
    assert detect_unclean_exit(home=tmp_path) is not None  # PID reused by another process: death

    # Pre-stamp sentinel (no create_time): the owner was born before it claimed; a reuser after.
    _write_sentinel(tmp_path, live)  # birth 5000.0 <= claim 5003.7 → owner
    assert detect_unclean_exit(home=tmp_path) is None
    _write_sentinel(tmp_path, {**live, "start_time": 4000.0})  # born after the claim → reuser → death
    assert detect_unclean_exit(home=tmp_path) is not None
