"""A launch-home alias must not create a second plugin service owner."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from hermes_constants import get_hermes_home


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [False, True])
async def test_launch_home_has_one_logical_service_owner(tmp_path, monkeypatch, alias):
    from gateway import run as gateway_run
    from hermes_cli import plugins

    launch = tmp_path / "launch"
    nested = tmp_path / "nested"
    launch.mkdir()
    nested.mkdir()
    duplicate = launch
    if alias:
        duplicate = tmp_path / "alias"
        duplicate.symlink_to(launch, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("HERMES_PROFILE", "logical-root")
    seen = []

    async def service(runtime):
        seen.append((runtime.profile_name, Path(runtime.profile_home), Path(get_hermes_home())))
        await runtime.stop_event.wait()

    monkeypatch.setattr(gateway_run, "_multiplex_profile_homes", lambda _: [
        ("default", duplicate), ("nested", nested),
    ])
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: SimpleNamespace(
        _profile_services=[("probe", service)] if Path(get_hermes_home()).resolve() == launch else [],
    ))
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    try:
        runner._start_plugin_profile_services()
        await asyncio.sleep(0)
        assert seen == [("logical-root", launch, launch)]
        assert len(runner._profile_service_tasks) == 1
    finally:
        await runner._stop_plugin_profile_services()


@pytest.mark.asyncio
async def test_repeated_start_does_not_orphan_existing_service(tmp_path, monkeypatch):
    from hermes_cli import plugins

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "logical-root")
    seen = []

    async def service(runtime):
        seen.append(runtime.profile_name)
        await runtime.stop_event.wait()

    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: SimpleNamespace(
        _profile_services=[("probe", service)],
    ))
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    try:
        runner._start_plugin_profile_services()
        await asyncio.sleep(0)
        original = runner._profile_service_tasks[0]
        runner._start_plugin_profile_services()
        await asyncio.sleep(0)
        assert seen == ["logical-root"]
        assert runner._profile_service_tasks == [original]
    finally:
        await runner._stop_plugin_profile_services()
