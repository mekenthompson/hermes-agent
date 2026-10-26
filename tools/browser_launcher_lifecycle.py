"""Native shared-browser retention and profile-bound teardown admission."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from pathlib import Path


@contextmanager
def cleanup_guard(home_key):
    # Old shared records without an owning profile cannot safely consult a
    # launch-profile lease. Retain them instead of guessing another profile.
    if not isinstance(home_key, str) or not Path(home_key).is_absolute():
        yield False
        return
    from tools.bot_desktop.lease import cleanup_guard as lease_guard

    with lease_guard(home_key) as admitted:
        yield admitted


def recorded_home(record, browser):
    """A shared browser stays on its launch home's display after holder handover."""
    from tools import browser_launcher as launcher

    data = launcher._load_record(record) if record.exists() else None
    if data is not None and launcher._record_is(data, browser):
        return data.get("home_key")
    return browser.home_key


def abandon_launch(proc, starting):
    """Release a failed/readiness-interrupted launch without evicting its viewer."""
    import shutil
    from tools import browser_launcher as launcher
    from tools.browser_launcher_holders import records_lock

    with launcher._lock:
        launcher._launching.pop(proc.pid, None)
    record = launcher._record_path(starting)
    # _start_shared already holds this lock across the readiness wait.
    lock = records_lock(record.parent) if record is not None and starting.ephemeral else nullcontext()
    with lock:
        if not starting.ephemeral and record is not None:
            if not launcher._last_holder_out(record, starting):
                return
        home = recorded_home(record, starting) if record is not None else starting.home_key
        with cleanup_guard(home) as admitted:
            if not admitted:
                return
            if proc.poll() is None:
                launcher._terminate(proc.pid, starting.start_time, proc)
            launcher._remove_record(starting)
            if starting.ephemeral:
                shutil.rmtree(starting.user_data_dir, ignore_errors=True)


def _retain_shared_browser(key: str, *, release_holder: bool) -> bool:
    """Release this caller's shared browser without closing its tabs/process.

    The verified launch record retains the browser intentionally, so the
    caller's ordinary exit/orphan sweep cannot mistake it for a crashed job.
    Explicit native browser stop remains available; browser state stays local.
    """
    from tools import browser_launcher as launcher
    from tools.browser_launcher_holders import records_lock
    from utils import atomic_json_write

    with launcher._key_lock(key):
        with launcher._lock:
            browser = launcher._browsers.get(key)
        if browser is None or browser.ephemeral:
            return False
        record = launcher._record_path(browser)
        if record is None:
            raise RuntimeError("Shared browser retention requires its private launch state")
        with records_lock(record.parent):
            data = launcher._load_record(record) if record.exists() else None
            if data is not None:
                if not launcher._record_is(data, browser):
                    raise RuntimeError("Shared browser launch changed before detach")
                atomic_json_write(record, {**data, "retained": True}, indent=None)
            elif browser.owned:
                raise RuntimeError("Owned shared browser has no launch record to retain")
            if release_holder:
                with launcher._lock:
                    launcher._forget(key, browser, drop_record=False)
        return True


def detach_shared_browser(key: str) -> bool:
    return _retain_shared_browser(key, release_holder=True)


def retain_shared_browser(key: str) -> bool:
    return _retain_shared_browser(key, release_holder=False)


def detach_profile_browsers(home_key, *, keep_keys=(), skip_keys=()):
    """Include persistent launchers used directly, outside the session cache."""
    from tools import browser_launcher as launcher

    with launcher._lock:
        keys = [key for key, browser in launcher._browsers.items()
                if browser.home_key == home_key and not browser.ephemeral and key not in skip_keys]
    return sum(_retain_shared_browser(key, release_holder=key not in keep_keys) for key in keys)


def _profile_occupied(directory):
    from tools import browser_launcher as launcher
    return launcher._live_local_holder(directory) is not None or launcher._foreign_holder(directory) is not None


def _retire_profile_locked(directory, key, home_key):
    """Caller holds the profile records lock through occupancy and deletion."""
    import shutil
    from tools import browser_launcher as launcher

    browser = launcher._attach_running(key, directory, home_key)
    if browser is None:
        if _profile_occupied(directory):
            return False
        with cleanup_guard(home_key) as admitted:
            if admitted and Path(directory).exists():
                shutil.rmtree(directory)
            return admitted
    with launcher._lock:
        launcher._browsers[key] = browser
    record = launcher._record_path(browser)
    try:
        if record is None:
            return False
        if launcher._heir_key(browser) is not None or not launcher._last_holder_out(record, browser, force=True):
            return False
        with cleanup_guard(recorded_home(record, browser)) as admitted:
            if not admitted:
                return False
            launcher._terminate(browser.pid, browser.start_time, None)
            if _profile_occupied(directory):
                return False
            record.unlink(missing_ok=True)
            if Path(directory).exists():
                shutil.rmtree(directory)
            return True
    finally:
        with launcher._lock:
            launcher._forget(key, browser, drop_record=False)


def retire_shared_profile(user_data_dir, profile_home):
    """Retire a copied profile atomically against native launches and takeover."""
    import os
    from hermes_constants import hermes_home_key
    from tools import browser_launcher as launcher
    from tools.browser_launcher_holders import records_lock

    directory = os.path.realpath(user_data_dir)
    key = 'retained-profile-retire:' + directory
    records = launcher._shared_state_dir(directory)
    if records is None:
        return False
    with launcher._key_lock(key):
        with records_lock(records):
            return _retire_profile_locked(directory, key, hermes_home_key(profile_home))
