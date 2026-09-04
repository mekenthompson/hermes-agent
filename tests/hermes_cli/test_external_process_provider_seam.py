"""An external-process (ACP) provider can ship from outside this tree.

The known-provider gate, the binary, the argv and the env var names used to be
spelled out for one vendor, so a profile registered from a plugin died with
"Unknown provider" before any client was built. This registers a provider the
way a standalone package does — before importing ``hermes_cli`` — and walks the
real resolution path, asserting ``copilot-acp`` is unchanged alongside it.
"""

from __future__ import annotations

import os
import stat

import pytest

from providers import get_provider_profile, register_provider
from providers.base import ProviderProfile


class _AcmeACPProfile(ProviderProfile):
    def create_client(self, **kwargs):
        return ("acme-client", kwargs)

    def fetch_models(self, **kwargs):
        return None


register_provider(
    _AcmeACPProfile(
        name="acme-acp",
        aliases=("acme",),
        display_name="Acme ACP",
        base_url="acp://acme",
        auth_type="external_process",
        process_command="acme-cli",
        process_args=("--acp",),
        process_command_env_vars=("ACME_CLI_PATH",),
        process_args_env_var="ACME_ACP_ARGS",
    )
)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("acme-cli", "copilot", "custom-acme"):
        exe = bindir / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bindir


def test_an_out_of_tree_external_process_provider_resolves_end_to_end(fake_cli, monkeypatch):
    from hermes_cli.auth import PROVIDER_REGISTRY, resolve_external_process_provider_credentials, resolve_provider
    from hermes_cli.runtime_provider import resolve_runtime_provider

    assert PROVIDER_REGISTRY["acme"] is PROVIDER_REGISTRY["acme-acp"]
    assert PROVIDER_REGISTRY["acme-acp"].auth_type == "external_process"
    assert resolve_provider("acme") == "acme-acp"

    creds = resolve_external_process_provider_credentials("acme-acp")
    assert (creds["command"], creds["args"], creds["api_key"]) == (str(fake_cli / "acme-cli"), ["--acp"], "acme-acp")

    monkeypatch.setenv("ACME_CLI_PATH", str(fake_cli / "custom-acme"))
    monkeypatch.setenv("ACME_ACP_ARGS", "--acp=true --verbose")
    creds = resolve_external_process_provider_credentials("acme-acp")
    assert (creds["command"], creds["args"]) == (str(fake_cli / "custom-acme"), ["--acp=true", "--verbose"])

    runtime = resolve_runtime_provider(requested="acme", target_model="acme")
    assert (runtime["provider"], runtime["base_url"], runtime["source"]) == ("acme-acp", "acp://acme", "process")


def test_provider_registered_after_auth_import_resolves_from_profile(fake_cli):
    from hermes_cli import auth

    late = _AcmeACPProfile(
        name="late-acp",
        display_name="Late ACP",
        base_url="acp://late",
        auth_type="external_process",
        process_command="acme-cli",
        process_args=("--late",),
    )
    register_provider(late)

    assert "late-acp" not in auth.PROVIDER_REGISTRY
    creds = auth.resolve_external_process_provider_credentials("late-acp")
    assert (creds["command"], creds["args"], creds["base_url"]) == (
        str(fake_cli / "acme-cli"),
        ["--late"],
        "acp://late",
    )

    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested="late-acp")
    assert runtime["provider"] == "late-acp"
    assert runtime["command"] == str(fake_cli / "acme-cli")
    assert runtime["args"] == ["--late"]


def test_static_and_profile_auth_types_must_agree(fake_cli, monkeypatch):
    from hermes_cli import auth

    conflict = _AcmeACPProfile(
        name="collision-acp",
        display_name="Collision ACP",
        base_url="acp://collision",
        auth_type="external_process",
        process_command="acme-cli",
    )
    register_provider(conflict)
    monkeypatch.setitem(
        auth.PROVIDER_REGISTRY,
        "collision-acp",
        auth.ProviderConfig(
            id="collision-acp",
            name="Collision ACP",
            auth_type="api_key",
            api_key_env_vars=("COLLISION_API_KEY",),
        ),
    )

    from hermes_cli.runtime_provider import resolve_runtime_provider
    with pytest.raises(auth.AuthError, match="conflicting authentication metadata"):
        resolve_runtime_provider(requested="collision-acp")


def test_profile_cannot_override_static_external_auth_type(fake_cli):
    from hermes_cli import auth

    original = get_provider_profile("copilot-acp")
    conflict = _AcmeACPProfile(
        name="copilot-acp",
        display_name="Conflicting Copilot",
        base_url="https://example.invalid",
        auth_type="api_key",
        process_command="acme-cli",
    )
    register_provider(conflict)
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        with pytest.raises(auth.AuthError, match="conflicting authentication metadata"):
            resolve_runtime_provider(requested="copilot-acp")
    finally:
        register_provider(original)


def test_copilot_acp_launch_details_are_unchanged(fake_cli, monkeypatch):
    from hermes_cli.auth import resolve_external_process_provider_credentials
    from hermes_cli.runtime_provider import resolve_runtime_provider

    creds = resolve_external_process_provider_credentials("copilot-acp")
    assert creds["command"] == str(fake_cli / "copilot")
    assert (creds["args"], creds["api_key"], creds["base_url"]) == (["--acp", "--stdio"], "copilot-acp", "acp://copilot")

    monkeypatch.setenv("COPILOT_CLI_PATH", str(fake_cli / "custom-acme"))
    assert resolve_external_process_provider_credentials("copilot-acp")["command"] == str(fake_cli / "custom-acme")
    assert resolve_runtime_provider(requested="copilot-acp", target_model="x")["base_url"] == "acp://copilot"
