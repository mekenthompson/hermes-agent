"""Local and remote browser configuration defaults, kept separate from the config facade."""


def default_browser_config() -> dict:
    """Return fresh browser defaults, including independently mutable argument lists."""
    return {
        # REMOVAL NOTE: `backend` (built-in browser_* tools vs browser_exec), `snapshot_threshold`,
        # `record_sessions`, `allow_unsafe_evaluate` and `restrict_evaluate` left with the built-in
        # browser tools; browser_exec is the only core browser. Config v52 drops `backend` (an `off`
        # gets a one-time notice) and copies AGENT_BROWSER_* from .env to the keys below; other
        # user values stay untouched.
        "inactivity_timeout": 120,
        # Seconds a local Chromium launch may take to open its DevTools port; a launch always waits
        # at least 120 s (a cold first start unpacks the profile).
        "command_timeout": 30,
        # headed: visible Chromium window (local); skips per-turn cleanup, idle reaper still applies
        "headed": False,
        "allow_private_urls": False,  # allow private/internal IPs (localhost, 192.168.x.x, ...)
        # Local browser engine. "auto" = Chromium; "lightpanda" = `lightpanda serve` per session
        # (faster navigation, no screenshots); "chrome" is read as "auto". Ignored while a cloud
        # provider, cdp_url or use_real_profile is active.
        "engine": "auto",
        # Chromium binary for local sessions. "" = pm's chromium (`hermes pm install chromium`), the
        # Docker image's build, else a system chromium where pm builds none (Termux).
        "executable_path": "",
        # Persistent Chromium profile directory shared by local sessions; "" = a fresh throwaway
        # profile per session.
        "user_data_dir": "",
        "sandbox_required": False,  # fail closed instead of auto-disabling Chromium's sandbox
        "chromium_args": [],  # extra Chromium flags for local sessions, e.g. ["--lang=de"]
        # With a cloud provider, auto-spawn local Chromium for LAN/localhost URLs instead
        "auto_local_for_private_urls": True,
        "cdp_url": "",  # persistent CDP endpoint for attaching to an existing Chromium/Chrome
        # Consent to browse with the user's REAL logins locally: runs on a Hermes-managed SNAPSHOT
        # of the ACTIVE default-Chromium profile (Local State -> profile.last_used; cookies, logins,
        # prefs copied and re-synced per fresh session) driven by Hermes' packaged Chromium. The
        # snapshot dir sidesteps Chrome 136+'s default-profile debugging block and never contends
        # with the running browser. Turning off deletes ~/.hermes/browser-profile/ so credentials
        # don't outlive consent. Chromium-family only; a profile that can't be copied (Firefox default,
        # Windows browser holding its cookies) gives a fresh, signed-out browser plus a note saying why.
        # Also gates the browser_exec `local` arg (real-profile session under a cloud backend). Desktop Settings -> Browser.
        "use_real_profile": False,
        # Windows only: a running Chrome/Edge/Brave locks its cookie DB (the last snapshot is reused;
        # with none yet, a fresh browser runs). When on, the agent ASKS first; on approval it runs
        # `hermes browser close-profile` (kills that profile's browser tree, unsaved tabs lost) so a
        # new session can copy it; no auto-kill. No effect on macOS/Linux, where a running
        # browser only locks the password/autofill DBs, which are skipped (cookies still copy).
        "real_profile_autoclose": False,
        # Pin WHICH source profile directory is snapshotted for real-profile browsing (e.g. "Profile
        # 2"). Empty = browser's last-used profile, which on multi-profile machines can hand the
        # agent the wrong identity. A pin naming a missing directory never falls back to another
        # profile: the session gets a fresh browser without the user's logins.
        "real_profile_pin": "",
        # CDP supervisor: dialog + frame detection over a persistent WebSocket; active only with a
        # CDP-capable backend (Browserbase, or local Chrome via /browser connect). See
        # website/docs/developer-guide/browser-supervisor.md.
        "dialog_policy": "must_respond",  # must_respond | auto_dismiss | auto_accept
        "dialog_timeout_s": 300,  # safety auto-dismiss after N seconds under must_respond
        # Read by the `camofox` catalog plugin (Camofox left core). An empty free-form mapping, so
        # `hermes config set browser.camofox.<key>` is accepted and user values are never stripped.
        "camofox": {},
    }
