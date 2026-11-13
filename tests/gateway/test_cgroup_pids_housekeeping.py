"""The housekeeping pids check turns a cgroup task ceiling into one errors.log WARNING per episode."""

import logging

import pytest

from gateway import run_housekeeping_pids as pids


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(pids, "_state", {"high": False, "max_events": 0})


def _cgroup(tmp_path, current, limit="512", max_events=0):
    (tmp_path / "pids.current").write_text(f"{current}\n")
    (tmp_path / "pids.max").write_text(f"{limit}\n")
    (tmp_path / "pids.events").write_text(f"max {max_events}\n")
    return tmp_path


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_warns_once_per_episode_above_eighty_percent(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=pids.__name__)
    root = _cgroup(tmp_path, 300)
    pids.check_cgroup_pids(root)
    assert _warnings(caplog) == []

    _cgroup(tmp_path, 420)  # 82% of 512
    pids.check_cgroup_pids(root)
    pids.check_cgroup_pids(root)
    warned = _warnings(caplog)
    assert len(warned) == 1 and "pids.current=420 of pids.max=512" in warned[0]

    _cgroup(tmp_path, 200)
    pids.check_cgroup_pids(root)
    _cgroup(tmp_path, 500)
    pids.check_cgroup_pids(root)
    assert len(_warnings(caplog)) == 2


def test_warns_when_kernel_refused_forks_since_last_check(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=pids.__name__)
    root = _cgroup(tmp_path, 100, max_events=0)
    pids.check_cgroup_pids(root)
    _cgroup(tmp_path, 100, max_events=127)
    pids.check_cgroup_pids(root)
    pids.check_cgroup_pids(root)
    warned = _warnings(caplog)
    assert len(warned) == 1 and "127 fork/thread start(s) refused" in warned[0]


def test_unlimited_or_missing_cgroup_is_silent(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=pids.__name__)
    pids.check_cgroup_pids(_cgroup(tmp_path, 100000, limit="max", max_events=5))
    pids.check_cgroup_pids(tmp_path / "absent")
    assert _warnings(caplog) == []


class _OneTickStopEvent:
    """Run one housekeeping tick without a sleep or background thread."""

    def __init__(self):
        self.waited = False

    def is_set(self):
        return self.waited

    def wait(self, timeout=None):
        self.waited = True
        return True


def test_housekeeping_tick_runs_the_pids_check(monkeypatch):
    import gateway.run as gateway_run

    calls = []
    monkeypatch.setattr(pids, "check_cgroup_pids", lambda: calls.append(True))
    gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0)
    assert calls == [True]
