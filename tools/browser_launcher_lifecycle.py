"""Native shared-browser retention and profile-bound teardown admission."""
from __future__ import annotations

from contextlib import contextmanager
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


def detach_shared_browser(key: str) -> bool:
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
            with launcher._lock:
                launcher._forget(key, browser, drop_record=False)
        return True
