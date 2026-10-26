"""Browser actions follow the lease of the browser's launch/display home."""
from __future__ import annotations

from pathlib import Path
import json

_SESSION_SENTINEL = "_HERMES_BU_LOCAL_SESSION"


def remember_session(env, session_info):
    # Internal routing metadata, consumed before the harness subprocess. Keep
    # the endpoint/launcher identity instead of reducing provenance to a bool.
    env[_SESSION_SENTINEL] = json.dumps({
        key: session_info[key] for key in ('launcher_key', 'cdp_url', 'home_key', 'features')
        if key in session_info})


def take_session(env):
    return json.loads(env.pop(_SESSION_SENTINEL, '{"features":{"local":true}}'))


def owner_home(session_info):
    from hermes_constants import hermes_home_key
    from tools import browser_launcher as launcher
    from tools.browser_launcher_lifecycle import recorded_home

    home = session_info.get("home_key") or hermes_home_key()
    key = session_info.get("launcher_key")
    browser = launcher.get_browser(key) if key else None
    if browser is not None and browser.cdp_url == session_info.get("cdp_url"):
        record = launcher._record_path(browser)
        home = recorded_home(record, browser) if record is not None else browser.home_key
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise RuntimeError("The native browser's owning profile could not be verified")
    return hermes_home_key(home)


def shares_desktop(session_info, home):
    if not (session_info.get("features") or {}).get("local"):
        return False
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import lease, runtime

    # Only the desktop metadata read changes scope. The caller's action and
    # secret scope remain its own, even when another home launched Chromium.
    token = set_hermes_home_override(home)
    try:
        return bool(runtime.published_env().get("DISPLAY")) or lease.human_holds(home)
    finally:
        reset_hermes_home_override(token)


def shares_bot_desktop_browser(session_info):
    if not (session_info.get("features") or {}).get("local"):
        return False
    return shares_desktop(session_info, owner_home(session_info))


def human_holds_shared_browser(session_info):
    if not (session_info.get("features") or {}).get("local"):
        return False
    from tools.bot_desktop import lease

    home = owner_home(session_info)
    return shares_desktop(session_info, home) and lease.human_holds(home)


def run_fenced(session_info, fn):
    if not (session_info.get("features") or {}).get("local"):
        return fn()
    from tools.bot_desktop import lease

    home = owner_home(session_info)
    if not shares_desktop(session_info, home):
        return fn()
    try:
        admitted = lease.assert_agent_may_act(home)
    except lease.HumanHasControl as exc:
        return {"success": False, "error": str(exc), "code": "human_has_control"}
    result = fn()
    if lease.get(home).epoch != admitted.epoch:
        return {"success": False, "code": "human_has_control",
                "error": "A human took over the bot's screen while this browser command ran; its result was "
                         "discarded. Tell the user what you need; retry once they hand back."}
    return result
