"""Integration coverage for profile-service plugin registration."""

import asyncio
from pathlib import Path

import pytest
import yaml

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


@pytest.fixture
def default_profile(monkeypatch):
    """Keep the default-profile integration assertion independent of ambient env."""
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_AGENT_PROFILE", raising=False)


def _write_profile_service_plugin(hermes_home: Path) -> None:
    plugin_dir = hermes_home / "plugins" / "profile_service_probe"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "profile_service_probe",
                "version": "0.1.0",
                "description": "Profile service registration probe",
            }
        )
    )
    (plugin_dir / "__init__.py").write_text(
        "async def service(runtime):\n"
        "    runtime.gateway.service_calls.append((runtime.profile_name, runtime.profile_home))\n"
        "    await runtime.stop_event.wait()\n"
        "\n"
        "def register(ctx):\n"
        "    ctx.register_profile_service('profile-probe', service)\n"
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["profile_service_probe"]}})
    )


@pytest.mark.asyncio
async def test_discovered_profile_service_factory_is_started_by_gateway(
    tmp_path, monkeypatch, default_profile
):
    """A real discovered plugin contributes a factory consumed by gateway startup."""
    import hermes_cli.plugins as plugins_mod

    hermes_home = tmp_path / "hermes"
    _write_profile_service_plugin(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: tmp_path / "bundled")
    monkeypatch.setattr(PluginManager, "_scan_entry_points", lambda self: [])

    manager = PluginManager()
    manager.discover_and_load()
    assert [name for name, _factory in manager._profile_services] == ["profile-probe"]

    # Gateway startup obtains the manager through the normal module-level accessor.
    monkeypatch.setattr(plugins_mod, "_plugin_managers_by_home", {})
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.service_calls = []

    runner._start_plugin_profile_services()
    await asyncio.sleep(0)

    assert runner.service_calls == [("default", str(hermes_home))]
    assert [task.get_name() for task in runner._profile_service_tasks] == [
        "profile-service:default:profile-probe"
    ]
    await runner._stop_plugin_profile_services()


def test_profile_service_registration_validates_inputs_and_allows_duplicate_names():
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="probe", key="probe"), manager)

    async def factory(_runtime):
        return None

    with pytest.raises(ValueError, match="profile service requires a name and callable factory"):
        context.register_profile_service("", factory)
    with pytest.raises(ValueError, match="profile service requires a name and callable factory"):
        context.register_profile_service("probe", object())

    context.register_profile_service("probe", factory)
    context.register_profile_service("probe", factory)
    assert manager._profile_services == [("probe", factory), ("probe", factory)]
