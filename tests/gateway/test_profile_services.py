"""Plugin profile-service registration and gateway startup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run import GatewayRunner
from hermes_cli.plugins import PluginContext, PluginManager


def test_plugin_context_registers_profile_service():
    manager = PluginManager.__new__(PluginManager)
    manager._profile_services = []
    ctx = PluginContext.__new__(PluginContext)
    ctx._manager = manager

    async def factory(_runtime):
        return None

    ctx.register_profile_service("linear-agent", factory)
    assert manager._profile_services == [("linear-agent", factory)]


@pytest.mark.asyncio
async def test_gateway_starts_registered_profile_services(monkeypatch):
    started = []

    async def factory(runtime):
        started.append(runtime.profile_name)
        await runtime.stop_event.wait()

    manager = SimpleNamespace(_profile_services=[("linear-agent", factory)])
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: "/tmp/hermes-home")
    monkeypatch.setenv("HERMES_PROFILE", "aggie")

    runner = object.__new__(GatewayRunner)
    runner._profile_service_stop = None
    runner._profile_service_tasks = []
    runner._start_plugin_profile_services()
    await asyncio.sleep(0)
    assert started == ["aggie"]
    assert runner._profile_service_tasks
    await runner._stop_plugin_profile_services()
