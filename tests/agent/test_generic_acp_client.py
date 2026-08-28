"""Regression coverage for plugin-registered ACP agents.

Core owns generic ACP transport; capability plugins own vendor launch metadata.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.acp_agent_registry import (
    ACP_AGENT_REGISTRY,
    ACPAgentEntry,
    is_acp_agent_available,
    register_acp_agent,
    resolve_agent_launch,
)
from agent.copilot_acp_client import ACPClient


def test_plugin_registered_claude_agent_resolves_command_and_scrubs_markers():
    original = dict(ACP_AGENT_REGISTRY)
    try:
        register_acp_agent(
            "claude",
            ACPAgentEntry(
                command="claude-agent-acp",
                env_unset=("CLAUDECODE", "ANTHROPIC_API_KEY"),
                display_name="Claude Code",
            ),
        )
        client = ACPClient(base_url="acp://claude", acp_cwd="/tmp")
        assert client.agent_name == "claude"
        assert client._acp_command == "claude-agent-acp"
        assert client._acp_args == []
        with patch(
            "agent.copilot_acp_client.hermes_subprocess_env",
            return_value={"CLAUDECODE": "nested", "ANTHROPIC_API_KEY": "key", "KEEP": "yes"},
        ):
            env = client._subprocess_env()
        assert "CLAUDECODE" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert env["KEEP"] == "yes"
    finally:
        ACP_AGENT_REGISTRY.clear()
        ACP_AGENT_REGISTRY.update(original)


def test_external_process_credentials_use_a_registered_provider_profile(monkeypatch):
    from types import SimpleNamespace

    import hermes_cli.auth as auth

    original = dict(ACP_AGENT_REGISTRY)
    try:
        register_acp_agent("claude", ACPAgentEntry(command="claude-agent-acp"))
        profile = SimpleNamespace(
            name="claude-acp",
            auth_type="external_process",
            base_url="acp://claude",
            display_name="Claude Code ACP",
        )
        monkeypatch.setattr("providers.get_provider_profile", lambda name: profile if name == "claude-acp" else None)
        monkeypatch.setattr("hermes_cli.auth.shutil.which", lambda command: f"/usr/bin/{command}")
        creds = auth.resolve_external_process_provider_credentials("claude-acp")
        assert creds == {
            "provider": "claude-acp",
            "api_key": "claude-acp",
            "base_url": "acp://claude",
            "command": "/usr/bin/claude-agent-acp",
            "args": [],
            "source": "process",
        }
    finally:
        ACP_AGENT_REGISTRY.clear()
        ACP_AGENT_REGISTRY.update(original)


def test_runtime_provider_dispatches_dynamic_external_process_profiles():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "hermes_cli" / "runtime_provider.py").read_text()
    assert "if is_external_process_provider(provider):" in source
    assert '"provider": provider,' in source


def test_explicit_plugin_provider_is_accepted_by_provider_resolution(monkeypatch):
    from types import SimpleNamespace

    import hermes_cli.auth as auth

    profile = SimpleNamespace(name="claude-acp", auth_type="external_process")
    monkeypatch.setattr("providers.get_provider_profile", lambda name: profile if name == "claude-acp" else None)
    assert auth.resolve_provider("claude-acp") == "claude-acp"


def test_explicit_registered_agent_command_still_applies_registry_environment_scrub():
    original = dict(ACP_AGENT_REGISTRY)
    try:
        register_acp_agent(
            "claude",
            ACPAgentEntry(
                command="claude-agent-acp",
                env_unset=("CLAUDECODE", "ANTHROPIC_API_KEY"),
            ),
        )
        client = ACPClient(
            base_url="acp://claude",
            command="/usr/bin/claude-agent-acp",
            args=[],
            acp_cwd="/tmp",
        )
        with patch(
            "agent.copilot_acp_client.hermes_subprocess_env",
            return_value={"CLAUDECODE": "nested", "ANTHROPIC_API_KEY": "key", "KEEP": "yes"},
        ):
            env = client._subprocess_env()
        assert "CLAUDECODE" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert env["KEEP"] == "yes"
    finally:
        ACP_AGENT_REGISTRY.clear()
        ACP_AGENT_REGISTRY.update(original)


def test_auxiliary_client_resolves_plugin_external_process_profile(monkeypatch):
    from types import SimpleNamespace

    from agent.auxiliary_client import resolve_provider_client

    original = dict(ACP_AGENT_REGISTRY)
    try:
        register_acp_agent(
            "claude",
            ACPAgentEntry(
                command="claude-agent-acp",
                env_unset=("CLAUDECODE", "ANTHROPIC_API_KEY"),
            ),
        )
        profile = SimpleNamespace(
            name="claude-acp",
            auth_type="external_process",
            base_url="acp://claude",
            display_name="Claude Code ACP",
        )
        monkeypatch.setattr(
            "providers.get_provider_profile",
            lambda name: profile if name == "claude-acp" else None,
        )
        monkeypatch.setattr(
            "hermes_cli.auth.shutil.which",
            lambda command: f"/usr/bin/{command}",
        )
        client, model = resolve_provider_client("claude-acp", model="claude-sonnet")
        assert isinstance(client, ACPClient)
        assert model == "claude-sonnet"
        assert client._acp_command == "/usr/bin/claude-agent-acp"
        with patch(
            "agent.copilot_acp_client.hermes_subprocess_env",
            return_value={"CLAUDECODE": "nested", "ANTHROPIC_API_KEY": "key", "KEEP": "yes"},
        ):
            env = client._subprocess_env()
        assert "CLAUDECODE" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert env["KEEP"] == "yes"
    finally:
        ACP_AGENT_REGISTRY.clear()
        ACP_AGENT_REGISTRY.update(original)


def test_unknown_agent_environment_override_does_not_authorize_execution(monkeypatch):
    monkeypatch.setenv(
        "HERMES_ACP_UNREGISTERED_COMMAND",
        "/bin/echo injected --acp",
    )
    assert not is_acp_agent_available("unregistered")
    with pytest.raises(ValueError, match="Unknown ACP agent"):
        resolve_agent_launch("unregistered")
    client = ACPClient(base_url="acp://unregistered", acp_cwd="/tmp")
    assert isinstance(client._registry_error, ValueError)
    assert client._acp_command == ""


def test_unknown_agent_explicit_command_is_rejected_before_process_spawn():
    client = ACPClient(
        base_url="acp://unregistered",
        command="/bin/echo",
        args=["injected", "--acp"],
        acp_cwd="/tmp",
    )
    with patch(
        "agent.copilot_acp_client.subprocess.Popen",
        side_effect=AssertionError("process spawn reached"),
    ) as popen:
        with pytest.raises(RuntimeError, match="Unknown ACP agent"):
            client._run_prompt("hello", timeout_seconds=0.1)
    popen.assert_not_called()

