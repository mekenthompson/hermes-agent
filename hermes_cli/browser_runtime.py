"""Read-only full-Chromium selection shared by browser launchers.

Nothing here installs, downloads or starts anything: a caller that may provision (the launcher's
one-shot ``pm.ensure("chromium")``) does so itself and then asks again.
"""

from __future__ import annotations

import os
from pathlib import Path

import pm

# Written by the Docker image build with the pm Chromium binary it staged. The binary's path differs
# per architecture (chrome-linux64/chrome vs chromium-linux-arm64/chromium), so the build resolves it
# once; it also survives a volume over the runtime dir that hides pm's facts.
BAKED_EXECUTABLE_FILE = Path("/etc/hermes/chromium-executable-path")

# Distribution command names for a system Chromium, used only where pm builds none for this target
# (Termux: `pkg install chromium` from x11-repo).
_SYSTEM_CHROMIUM_COMMANDS = ("chromium", "chromium-browser")


def _configured_executable() -> str | None:
    """``browser.executable_path`` from the active profile's config (managed overlay applied)."""
    from hermes_cli.config import cfg_get, load_config_readonly

    value = cfg_get(load_config_readonly(), "browser", "executable_path", default="")
    value = os.path.expanduser(value.strip()) if isinstance(value, str) else ""
    return value or None


def _pm_executable() -> str | None:
    installed = pm.installed_package("chromium")
    return str(installed.binary) if installed and installed.binary is not None else None


def _baked_executable() -> str | None:
    try:
        value = BAKED_EXECUTABLE_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:  # not a Hermes Docker image: nothing baked
        return None
    return value if value and os.path.isfile(value) else None


def _pm_has_no_chromium() -> bool:
    """pm publishes no Chromium for this target (Termux/Android, musl)."""
    return pm.get_package("chromium").missing_reason(pm.current_target()) is not None


def _system_executable() -> str | None:
    from hermes_platform.resolver import locate_command

    for name in _SYSTEM_CHROMIUM_COMMANDS:
        found = locate_command(name).command
        if found:
            return found[0]
    return None


def chromium_executable(*, allow_override: bool = True) -> str | None:
    """The full Chromium a browser launcher should run, or ``None`` when none is available.

    Order: ``browser.executable_path`` (skipped with ``allow_override=False``, for callers that need
    pm's own build) -> pm's installed ``chromium`` -> the Docker image's baked path -> a system
    ``chromium`` / ``chromium-browser`` on PATH, only on targets pm builds no Chromium for (Termux).
    """
    if allow_override and (override := _configured_executable()):
        return override
    if found := _pm_executable() or _baked_executable():
        return found
    return _system_executable() if _pm_has_no_chromium() else None


def chromium_sandbox_required() -> bool:
    """Whether this profile forbids automatic Chromium sandbox bypass."""
    from hermes_cli.config import cfg_get, load_config_readonly

    return cfg_get(load_config_readonly(), "browser", "sandbox_required", default=False) is True


def validate_chromium_sandbox_args(argv) -> None:
    """Refuse explicit sandbox-disabling switches under the profile's required policy."""
    if not chromium_sandbox_required():
        return
    forbidden = {
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-seccomp-filter-sandbox",
        "--disable-namespace-sandbox", "--single-process", "--no-zygote",
        "--disable-gpu-sandbox", "--in-process-gpu",
    }
    if any(str(arg).split("=", 1)[0] in forbidden for arg in argv):
        raise ValueError("browser.sandbox_required forbids sandbox-disabling Chromium flags; "
                         "remove those flags and configure the container's sandbox policy.")


def _verify_existing_renderer_sandbox(process) -> None:
    """Require kernel evidence, not merely the absence of disabling argv switches.

    Linux /proc exposes PID namespace depth and the renderer's additional seccomp
    filters. Missing/unreadable evidence (including other platforms) refuses reuse.
    This checks a local Chromium process, not a remote endpoint or hostile host.
    """
    def status(pid):
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        return {key: value.strip() for line in text.splitlines()
                if ":" in line for key, value in [line.split(":", 1)]}

    parent = status(process.pid)
    parent_filters = int(parent["Seccomp_filters"])
    parent_depth = len(parent["NSpid"].split())
    renderers = []
    for child in process.children(recursive=True):
        argv = " ".join(child.cmdline()).split()
        if "--type=renderer" not in argv:
            continue
        validate_chromium_sandbox_args(argv)
        observed = status(child.pid)
        if not (observed["Uid"].split() == parent["Uid"].split()
                and "0" not in observed["Uid"].split()
                and observed["NoNewPrivs"] == "1"
                and observed["Seccomp"] == "2"
                and int(observed["Seccomp_filters"]) > parent_filters
                and int(observed["CapEff"], 16) == 0
                and len(observed["NSpid"].split()) > parent_depth
                and child.is_running()):
            raise ValueError("existing renderer sandbox evidence is insufficient")
        renderers.append(child.pid)
    if not renderers or not process.is_running():
        raise ValueError("existing browser has no verifiable sandboxed renderer; close it normally and retry")


def chromium_existing_sandbox_error(pid: int | None, *, foreign: bool = False) -> str | None:
    """Do not adopt an unverifiable or explicitly unsandboxed existing browser."""
    if not chromium_sandbox_required():
        return None
    import psutil

    if pid is None or foreign:
        return "browser.sandbox_required: cannot verify the existing browser; leave it running and close it normally."
    try:
        process = psutil.Process(pid)
        argv = process.cmdline()
        if not argv:
            raise ValueError("browser.sandbox_required: existing browser command line is unavailable.")
        validate_chromium_sandbox_args(argv)
        _verify_existing_renderer_sandbox(process)
    except (psutil.Error, OSError, ValueError, KeyError) as exc:
        return f"browser.sandbox_required: refusing existing browser without changing its profile: {exc}"
    return None


def chromium_missing_hint() -> str:
    """One actionable line for "no Chromium" on this machine. Never suggests npm."""
    from hermes_platform.host.runtime import is_termux

    if pinned := _configured_executable():  # the pin wins over every other source, so name it
        return (f"browser.executable_path is set to {pinned}, which is not a browser on this machine: fix the "
                "path, or clear it to use Hermes's Chromium.")
    if is_termux():
        return ("No Chromium found: pm builds none for Termux. Install one with "
                "`pkg install x11-repo && pkg install chromium`, or set browser.executable_path.")
    if BAKED_EXECUTABLE_FILE.exists():
        return ("The image's Chromium is missing: pull the current Hermes image and recreate the "
                "container (a volume mounted over the image's tool store hides it).")
    if _pm_has_no_chromium():
        return ("No Chromium found: pm builds none for this platform. Install your distribution's "
                "chromium package, or set browser.executable_path.")
    return "Chromium is not installed: run `hermes pm install chromium`."
