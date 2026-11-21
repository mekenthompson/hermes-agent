"""``MaxInProgressTracker``: a bad ``kanban.max_in_progress`` keeps the last good cap (HF-789)."""

from __future__ import annotations

import pytest

import hermes_cli.config as cfgmod
import hermes_cli.config_effective as cfg_effective
from hermes_cli.kanban_db_max_in_progress import (
    MaxInProgressTracker,
    configured_max_in_progress,
    read_max_in_progress_setting,
)


@pytest.fixture
def config(monkeypatch):
    """Mutable merged + user-layer config the tracker reads every resolve()."""
    merged: dict = {"kanban": {}}
    user: dict = {}
    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: merged)
    monkeypatch.setattr(cfg_effective, "load_user_config_effective", lambda *a, **k: user)
    return merged, user


@pytest.mark.parametrize(
    ("merged", "user", "expected"),
    [
        ({"kanban": {"max_in_progress": 4}}, {}, ("valid", 4)),
        ({"kanban": {"max_in_progress": "5"}}, {}, ("valid", 5)),
        ({"kanban": {"max_in_progress": None}}, {}, ("absent", None)),
        ({"kanban": {}}, {}, ("absent", None)),
        ({}, {}, ("absent", None)),
        ({"kanban": {"max_in_progress": None}}, {"kanban": {"max_in_progress": None}}, ("invalid", None)),
        ({"kanban": {"max_in_progress": "abc"}}, {}, ("invalid", None)),
        ({"kanban": {"max_in_progress": 0}}, {}, ("invalid", None)),
        ({"kanban": {"max_in_progress": -1}}, {}, ("invalid", None)),
        ({"kanban": {"max_in_progress": ""}}, {}, ("invalid", None)),
        ({"kanban": {"max_in_progress": True}}, {}, ("invalid", None)),
        ({"kanban": "oops"}, {}, ("invalid", None)),
    ],
)
def test_read_max_in_progress_setting_classifies(monkeypatch, merged, user, expected):
    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: merged)
    monkeypatch.setattr(cfg_effective, "load_user_config_effective", lambda *a, **k: user)
    state, cap, _raw = read_max_in_progress_setting()
    assert (state, cap) == expected
    assert configured_max_in_progress() == (cap if state == "valid" else None)


def test_config_read_error_is_invalid_not_absent(monkeypatch):
    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(cfgmod, "load_config_readonly", boom)
    assert read_max_in_progress_setting()[0] == "invalid"


def test_tracker_warns_once_per_bad_value_and_picks_up_valid(config, caplog):
    merged, _user = config
    merged["kanban"]["max_in_progress"] = 3
    tracker = MaxInProgressTracker()
    caplog.set_level("WARNING")

    caps = [tracker.resolve(lambda: 99)]
    merged["kanban"]["max_in_progress"] = "abc"
    caps += [tracker.resolve(lambda: 99) for _ in range(5)]
    merged["kanban"]["max_in_progress"] = -1
    caps += [tracker.resolve(lambda: 99) for _ in range(3)]
    merged["kanban"]["max_in_progress"] = 5
    caps.append(tracker.resolve(lambda: 99))

    assert caps == [3] * 9 + [5]
    warnings = [r.getMessage() for r in caplog.records if "invalid kanban.max_in_progress" in r.getMessage()]
    assert len(warnings) == 2, warnings
    assert "keeping last good cap 3" in warnings[0]


def test_tracker_seeded_with_boot_cap_keeps_it_on_invalid(config):
    merged, _user = config
    merged["kanban"]["max_in_progress"] = 0
    assert MaxInProgressTracker(2).resolve(lambda: None) == 2


def test_tracker_invalid_with_no_good_cap_uses_derived_default(config):
    merged, _user = config
    merged["kanban"]["max_in_progress"] = "abc"
    tracker = MaxInProgressTracker()
    assert tracker.resolve(lambda: 4) == 4
    # The derived default is now the last good cap: a later memory-unknown
    # tick never drops it to unlimited.
    assert tracker.resolve(lambda: None) == 4


def test_tracker_absent_uses_derived_default_but_never_loosens_to_unlimited(config):
    tracker = MaxInProgressTracker(3)
    assert tracker.resolve(lambda: 6) == 6
    assert tracker.resolve(lambda: None) == 6
    assert MaxInProgressTracker().resolve(lambda: None) is None


@pytest.mark.parametrize("initial", [0, -1, True, "3"])
def test_tracker_ignores_invalid_seed(config, initial):
    merged, _user = config
    merged["kanban"]["max_in_progress"] = "abc"
    assert MaxInProgressTracker(initial).resolve(lambda: 7) == 7  # type: ignore[arg-type]
