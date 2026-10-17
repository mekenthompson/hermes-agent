from __future__ import annotations

from hermes_cli import providers as provider_catalog
from hermes_cli.providers import ProviderDef, get_provider, is_aggregator


def test_concrete_profile_wins_catalog_collision_without_losing_overlay_semantics(monkeypatch):
    """A profile owns its transport/endpoint, while Hermes overlay metadata remains authoritative."""
    profile = ProviderDef(
        id="openrouter", name="Registered OpenRouter", transport="commandcode",
        api_key_env_vars=("CMD_API_KEY",), base_url="https://profile.example/v1",
        base_url_env_var="", is_aggregator=False, auth_type="api_key", source="plugin-profile",
    )
    monkeypatch.setattr(provider_catalog, "_plugin_profile_pdef", lambda _name: profile)
    monkeypatch.setattr(provider_catalog, "_models_dev_info", lambda *_args, **_kwargs: object())

    resolved = get_provider("openrouter", allow_network=False)

    assert resolved is not None
    assert (resolved.source, resolved.transport, resolved.base_url) == (
        "plugin-profile", "commandcode", "https://profile.example/v1")
    assert resolved.is_aggregator is True
    assert resolved.base_url_env_var == "OPENROUTER_BASE_URL"
    assert is_aggregator("openrouter") is True


def test_registered_profile_key_and_transport_survive_catalog_collision(monkeypatch):
    """The catalog collision cannot replace the registered provider's identity or wire dialect."""
    profile = ProviderDef(
        id="commandcode", name="CommandCode", transport="commandcode",
        api_key_env_vars=("CMD_API_KEY",), base_url="https://commandcode.example/v1",
        base_url_env_var="CMD_BASE_URL", is_aggregator=False, auth_type="api_key", source="plugin-profile",
    )
    monkeypatch.setattr(provider_catalog, "_plugin_profile_pdef", lambda _name: profile)
    monkeypatch.setattr(provider_catalog, "_models_dev_info", lambda *_args, **_kwargs: object())
    monkeypatch.setitem(provider_catalog.HERMES_OVERLAYS, "commandcode", provider_catalog.HermesOverlay())

    resolved = get_provider("commandcode", allow_network=False)

    assert resolved is not None
    assert resolved.id == "commandcode"
    assert resolved.transport == "commandcode"
    assert resolved.base_url == profile.base_url
