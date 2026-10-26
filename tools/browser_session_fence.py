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


def take_session(env, local_engine):
    return json.loads(env.pop(_SESSION_SENTINEL, json.dumps({'features': {'local': bool(local_engine)}})))


def owner_home(session_info):
    from hermes_constants import hermes_home_key
    from tools import browser_launcher as launcher
    from tools.browser_launcher_lifecycle import recorded_home

    home = session_info.get("display_home_key") or session_info.get("home_key") or hermes_home_key()
    key = session_info.get("launcher_key")
    browser = launcher.get_browser(key) if key else None
    if browser is not None and browser.cdp_url == session_info.get("cdp_url"):
        record = launcher._record_path(browser)
        home = recorded_home(record, browser) if record is not None else browser.home_key
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise RuntimeError("The native browser's owning profile could not be verified")
    return hermes_home_key(home)


def connection_session(session_info, cdp_url):
    """Capture verified display provenance before publishing a vault connection."""
    from hermes_constants import hermes_home_key
    from tools import browser_launcher

    info = {key: session_info[key] for key in ('launcher_key', 'features') if key in session_info}
    info.update(cdp_url=cdp_url, home_key=hermes_home_key())
    if (info.get('features') or {}).get('local'):
        info['display_home_key'] = owner_home(info)
        key = info.get('launcher_key')
        browser = browser_launcher.get_browser(key) if key else None
        info['shared_profile'] = browser is not None and not browser.ephemeral
    return info


def profile_matches(session_info):
    from hermes_constants import hermes_home_key

    home = session_info.get('home_key')
    return home is None or hermes_home_key(home) == hermes_home_key()


def run_vault_io(supervisor, task_id, operation):
    """Re-admit page I/O on its captured socket after prompts or connection replacement."""
    from tools.browser_session import session_for_task

    info = supervisor.browser_session_info
    if info is None:
        info = session_for_task(task_id)
    return run_fenced(info, operation)


def connect_vault(task_id, info, endpoint, policy, timeout):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    def attach():
        return {'supervisor': SUPERVISOR_REGISTRY.get_or_start(
            task_id=task_id, cdp_url=endpoint, dialog_policy=policy, dialog_timeout_s=timeout,
            session_info=connection_session(info, endpoint))}

    return run_fenced(info, attach).get('supervisor')


def evaluate_vault(supervisor, task_id, expression):
    result = run_vault_io(supervisor, task_id, lambda: supervisor.evaluate_runtime(expression))
    if result.get('code'):
        return {'success': False, 'error_type': result['code'], 'error': result.get('error', '')}
    if result.get('ok'):
        return {'success': True, 'result': result.get('result')}
    error = str(result.get('error') or 'evaluate failed')
    return {'success': False, 'error_type': 'supervisor_required' if 'supervisor' in error.lower() else 'eval_failed',
            'error': error}


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
    if not profile_matches(session_info):
        return {'success': False, 'code': 'browser_profile_mismatch',
                'error': 'This browser connection belongs to another profile. Open a browser in this profile first.'}
    if not (session_info.get("features") or {}).get("local"):
        return fn()
    from tools.bot_desktop import lease

    home = owner_home(session_info)
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
