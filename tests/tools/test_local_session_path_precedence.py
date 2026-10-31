"""Login snapshots retain the explicitly selected session toolchain."""

import os
import shlex

import pytest

from tools.environments.local import LocalEnvironment


@pytest.mark.platforms("posix")
def test_selected_toolchain_wins_after_shell_init(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    login = tmp_path / "login"
    selected.mkdir()
    login.mkdir()
    for directory, marker in ((selected, "selected"), (login, "login")):
        executable = directory / "hf_path_probe"
        executable.write_text(f"#!/bin/sh\nprintf '{marker}\\n'\n")
        executable.chmod(0o700)
    extra = login / "hf_login_only"
    extra.write_text("#!/bin/sh\nprintf 'login-only\\n'\n")
    extra.chmod(0o700)
    init_file = tmp_path / "init.sh"
    init_file.write_text(f"export PATH={shlex.quote(str(login))}:/usr/bin:/bin\n")
    monkeypatch.setenv("PATH", str(selected) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(
        "tools.environments.local._read_terminal_shell_init_config",
        lambda: ([str(init_file)], False),
    )
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        first = env.execute("hf_path_probe; hf_login_only")
        second = env.execute("hf_path_probe; hf_login_only")
    finally:
        env.cleanup()

    for result in (first, second):
        assert result["returncode"] == 0
        assert result["output"].splitlines() == ["selected", "login-only"]
