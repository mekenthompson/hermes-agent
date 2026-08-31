"""Unit tests for the generic ACP client (issue #5257).

``ACPClient`` is agent-agnostic: it drives whatever the registry resolves
``agent_name`` to. Only ``copilot`` ships registered in core — the "claude"
and "codex" entries these tests exercise stand in for the plugin-registered
agents shipped by https://github.com/mvdbastos/hermes-acp-agents (see
``agent/acp_agent_registry.py``), registered here the same way that plugin's
``__init__.py`` would via ``register_acp_agent``, so the client's dispatch
logic — fallback bins, env stripping, install hints — is covered without
hardcoding third-party agents into core.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from agent.acp_agent_registry import ACP_AGENT_REGISTRY, ACPAgentEntry, register_acp_agent
from agent.acp_client import (
    ACPClient,
    create_acp_client,
    extract_agent_from_url,
    marker_base_url,
)
from agent.copilot_acp_client import ACP_MARKER_BASE_URL, CopilotACPClient


@pytest.fixture(autouse=True)
def _acp_test_agents():
    """Register the "claude"/"codex" entries hermes-acp-agents ships.

    Autouse + function-scoped so every test in this module sees them and
    nothing leaks into other test modules' global ``ACP_AGENT_REGISTRY``.
    """
    register_acp_agent("claude", ACPAgentEntry(
        command="claude-agent-acp",
        command_fallbacks=("claude-code-acp",),
        env_unset=("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"),
        display_name="Claude Code",
        install_hint=(
            "Install the maintained adapter: "
            "npm install -g @agentclientprotocol/claude-agent-acp "
            "(the older @zed-industries/claude-code-acp bin also works)."
        ),
    ))
    register_acp_agent("codex", ACPAgentEntry(
        command="codex-acp",
        display_name="Codex CLI",
        install_hint="npm install -g @zed-industries/codex-acp",
    ))
    try:
        yield
    finally:
        ACP_AGENT_REGISTRY.pop("claude", None)
        ACP_AGENT_REGISTRY.pop("codex", None)


def test_extract_agent_from_url():
    assert extract_agent_from_url("acp://copilot") == "copilot"
    assert extract_agent_from_url("acp://claude") == "claude"
    assert extract_agent_from_url("ACP://Claude") == "claude"
    assert extract_agent_from_url("acp://gemini/extra") == "gemini"
    assert extract_agent_from_url("https://api.openai.com/v1") is None
    assert extract_agent_from_url("") is None
    assert extract_agent_from_url(None) is None
    assert extract_agent_from_url("acp://") is None


def test_marker_base_url():
    assert marker_base_url("claude") == "acp://claude"
    assert marker_base_url(" Codex ") == "acp://codex"


def test_agent_name_derived_from_base_url():
    # Nothing installed on PATH -> registry resolves to the preferred bin.
    with patch("agent.acp_agent_registry.shutil.which", return_value=None):
        client = ACPClient(base_url="acp://claude", acp_cwd="/tmp")
    assert client.agent_name == "claude"
    assert client.agent_display_name == "Claude Code"
    assert client._acp_command == "claude-agent-acp"
    assert client.api_key == "claude-acp"


def test_agent_name_param_used_when_no_base_url():
    register_acp_agent("widget", ACPAgentEntry(command="widget", args=("--experimental-acp",)))
    try:
        client = ACPClient(agent_name="widget", acp_cwd="/tmp")
    finally:
        ACP_AGENT_REGISTRY.pop("widget", None)
    assert client.base_url == "acp://widget"
    assert client._acp_command == "widget"
    assert client._acp_args == ["--experimental-acp"]


def test_explicit_command_keeps_registry_default_args():
    client = ACPClient(agent_name="copilot", command="/opt/bin/copilot", acp_cwd="/tmp")
    assert client._acp_command == "/opt/bin/copilot"
    assert client._acp_args == ["--acp", "--stdio"]


def test_explicit_args_override_registry_defaults():
    client = ACPClient(agent_name="copilot", acp_args=["--acp"], acp_cwd="/tmp")
    assert client._acp_args == ["--acp"]


def test_unknown_agent_without_command_raises():
    with pytest.raises(ValueError):
        ACPClient(agent_name="mystery-agent", acp_cwd="/tmp")


def test_unknown_agent_with_explicit_command_is_allowed():
    client = ACPClient(agent_name="mystery-agent", command="/opt/bin/mystery", acp_cwd="/tmp")
    assert client._acp_command == "/opt/bin/mystery"
    assert client._acp_args == []


def test_factory_returns_copilot_subclass_for_copilot():
    client = create_acp_client(agent_name="copilot", acp_cwd="/tmp")
    assert isinstance(client, CopilotACPClient)
    assert client.base_url == ACP_MARKER_BASE_URL


def test_factory_returns_generic_client_for_other_agents():
    client = create_acp_client(agent_name="claude", acp_cwd="/tmp")
    assert isinstance(client, ACPClient)
    assert not isinstance(client, CopilotACPClient)


def test_factory_infers_agent_from_base_url():
    client = create_acp_client(base_url="acp://codex", acp_cwd="/tmp")
    assert client.agent_name == "codex"
    assert not isinstance(client, CopilotACPClient)


def test_copilot_subclass_passes_generic_isinstance_check():
    # auxiliary_client routes on isinstance(x, ACPClient)
    assert isinstance(CopilotACPClient(acp_cwd="/tmp"), ACPClient)


def test_missing_command_error_includes_install_hint():
    client = ACPClient(agent_name="claude", acp_cwd="/tmp")
    with patch("agent.acp_client.subprocess.Popen", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError) as excinfo:
            client._run_prompt("hi", timeout_seconds=1.0)
    message = str(excinfo.value)
    assert "Claude Code" in message
    assert "@agentclientprotocol/claude-agent-acp" in message


def test_claude_bridge_prefers_official_but_falls_back_to_zed():
    # Only the older Zed bin is installed -> registry resolves to it.
    def _only_zed(name):
        return "/usr/bin/claude-code-acp" if name == "claude-code-acp" else None

    with patch("agent.acp_agent_registry.shutil.which", side_effect=_only_zed):
        client = ACPClient(agent_name="claude", acp_cwd="/tmp")
    assert client._acp_command == "claude-code-acp"


def test_claude_env_unset_strips_session_markers():
    # The claude agent declares the Claude Code session markers to strip so
    # the bridge launches even inside a parent Claude Code session.
    from agent.acp_agent_registry import agent_env_unset
    from agent.acp_client import _build_subprocess_env

    markers = agent_env_unset("claude")
    assert "CLAUDECODE" in markers

    with patch.dict(
        "os.environ",
        {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SSE_PORT": "9"},
    ):
        env = _build_subprocess_env(env_unset=markers)
    for marker in markers:
        assert marker not in env
    # Non-claude agents strip nothing.
    assert agent_env_unset("codex") == ()


def test_early_exit_hook_default_is_generic(monkeypatch):
    client = ACPClient(agent_name="claude", acp_cwd="/tmp")
    assert client._early_exit_error("some stderr") is None


def test_copilot_early_exit_hook_detects_deprecated_extension():
    client = CopilotACPClient(acp_cwd="/tmp")
    stderr = "gh-copilot has been deprecated, no commands will be executed"
    message = client._early_exit_error(stderr)
    assert message and "deprecated" in message
    assert client._early_exit_error("unrelated stderr noise") is None


# ── session/request_permission ──────────────────────────────────────────
# Regression cover for the blanket-deny posture that made every gated
# command fail with "Tool use aborted" under claude-acp.

_PERMISSION_OPTIONS = [
    {"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"},
    {"optionId": "allow_always", "kind": "allow_always", "name": "Allow always"},
    {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
]


def _permission_params(command="docker start c1", options=None):
    return {
        "options": _PERMISSION_OPTIONS if options is None else options,
        "toolCall": {
            "title": "Bash",
            "kind": "execute",
            "rawInput": {"command": command, "description": "start a container"},
        },
    }


def _outcome(response):
    return response["result"]["outcome"]


def _client():
    return ACPClient(agent_name="claude", acp_cwd="/tmp")


def test_extract_permission_command_prefers_raw_input():
    from agent.acp_client import _extract_permission_command

    command, description = _extract_permission_command(
        {"title": "Bash", "rawInput": {"command": "ls -la", "description": "list"}}
    )
    assert command == "ls -la"
    assert description == "list"


def test_extract_permission_command_falls_back_to_title():
    from agent.acp_client import _extract_permission_command

    command, _ = _extract_permission_command({"title": "git push", "kind": "execute"})
    assert command == "git push"
    assert _extract_permission_command({}) == ("", "")
    assert _extract_permission_command(None) == ("", "")


def test_permission_deny_mode_cancels():
    with patch("agent.acp_client._acp_permission_mode", return_value="deny"):
        response = _client()._decide_permission(1, _permission_params())
    assert _outcome(response) == {"outcome": "cancelled"}


def test_permission_allow_mode_selects_allow_option():
    with patch("agent.acp_client._acp_permission_mode", return_value="allow"):
        response = _client()._decide_permission(1, _permission_params())
    assert _outcome(response) == {"outcome": "selected", "optionId": "allow_once"}


def test_permission_allow_mode_still_denies_unforwarded_mcp_server():
    client = _client()
    assert client._forwarded_mcp_servers == set()
    params = {
        "toolCall": {"toolName": "mcp__unforwarded__exfiltrate"},
        "options": _permission_params()["options"],
    }
    with patch("agent.acp_client._acp_permission_mode", return_value="allow"):
        response = client._decide_permission(1, params)
    assert _outcome(response) == {"outcome": "selected", "optionId": "deny"}


def test_permission_allow_mode_allows_forwarded_mcp_server():
    client = _client()
    client._forwarded_mcp_servers = {"github"}
    params = {
        "toolCall": {"toolName": "mcp__github__create_issue"},
        "options": _permission_params()["options"],
    }
    with patch("agent.acp_client._acp_permission_mode", return_value="allow"):
        response = client._decide_permission(1, params)
    assert _outcome(response) == {"outcome": "selected", "optionId": "allow_once"}


def test_permission_bridge_mode_approves_via_hermes_policy():
    with patch("agent.acp_client._acp_permission_mode", return_value="bridge"), patch(
        "tools.approval.check_dangerous_command", return_value={"approved": True}
    ) as gate:
        response = _client()._decide_permission(1, _permission_params())
    assert _outcome(response) == {"outcome": "selected", "optionId": "allow_once"}
    # Guards must stay on: a sandboxed env_type would auto-approve everything.
    assert gate.call_args.args == ("docker start c1", "local")


def test_permission_bridge_mode_denies_via_hermes_policy():
    with patch("agent.acp_client._acp_permission_mode", return_value="bridge"), patch(
        "tools.approval.check_dangerous_command",
        return_value={"approved": False, "message": "blocked"},
    ):
        response = _client()._decide_permission(1, _permission_params())
    assert _outcome(response) == {"outcome": "selected", "optionId": "deny"}


def test_permission_denial_cancels_when_no_reject_option_offered():
    only_allow = [{"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"}]
    with patch("agent.acp_client._acp_permission_mode", return_value="bridge"), patch(
        "tools.approval.check_dangerous_command", return_value={"approved": False}
    ):
        response = _client()._decide_permission(
            1, _permission_params(options=only_allow)
        )
    assert _outcome(response) == {"outcome": "cancelled"}


def test_permission_without_identifiable_command_denies():
    with patch("agent.acp_client._acp_permission_mode", return_value="bridge"):
        response = _client()._decide_permission(1, {"options": _PERMISSION_OPTIONS})
    assert _outcome(response) == {"outcome": "cancelled"}


def test_permission_failure_is_fail_safe():
    with patch("agent.acp_client._acp_permission_mode", return_value="bridge"), patch(
        "tools.approval.check_dangerous_command", side_effect=RuntimeError("boom")
    ):
        response = _client()._decide_permission(1, _permission_params())
    assert _outcome(response) == {"outcome": "cancelled"}


def test_acp_permission_mode_defaults_to_bridge(monkeypatch):
    from agent.acp_client import _acp_permission_mode

    with patch("tools.approval._get_approval_config", return_value={}):
        assert _acp_permission_mode() == "bridge"
    with patch("tools.approval._get_approval_config", return_value={"acp_mode": "DENY"}):
        assert _acp_permission_mode() == "deny"
    # Unrecognised values fall back to the default rather than failing open.
    with patch("tools.approval._get_approval_config", return_value={"acp_mode": "nope"}):
        assert _acp_permission_mode() == "bridge"


def test_acp_permission_mode_ignores_env_override(monkeypatch):
    """The permission mode is config-only; no environment variable may widen it.

    An earlier revision let HERMES_ACP_PERMISSION_MODE win over config.yaml.
    That is the wrong channel for a switch deciding whether an ACP backend may
    take side effects: it is invisible in config, silently inherited by
    subprocesses, and leaves no audit trail. Pin the removal so it cannot be
    reintroduced as a convenience.
    """
    from agent.acp_client import _acp_permission_mode

    monkeypatch.setenv("HERMES_ACP_PERMISSION_MODE", "allow")
    with patch("tools.approval._get_approval_config", return_value={"acp_mode": "deny"}):
        assert _acp_permission_mode() == "deny"


# ── --acp support probe (ported from the Copilot client: #87308 / #87309) ──


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    from agent.acp_client import _ACP_PROBE_CACHE

    _ACP_PROBE_CACHE.clear()
    yield
    _ACP_PROBE_CACHE.clear()


def _completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_probe_true_when_help_advertises_acp():
    from agent.acp_client import _acp_supported

    with patch(
        "agent.acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: copilot [--acp] [--stdio]"),
    ):
        assert _acp_supported("copilot", ["--acp", "--stdio"]) is True


def test_probe_false_when_help_lacks_acp():
    from agent.acp_client import _acp_supported

    with patch(
        "agent.acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: claude [--print] [--model]"),
    ):
        assert _acp_supported("claude", ["--acp", "--stdio"]) is False


def test_run_prompt_fast_fails_when_probe_says_unsupported():
    client = ACPClient(agent_name="copilot", api_key="k", base_url=marker_base_url("copilot"))
    with patch(
        "agent.acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: copilot [--print]"),
    ):
        with patch("agent.acp_client.subprocess.Popen") as popen:
            with pytest.raises(RuntimeError, match="ACP transport not supported"):
                client._run_prompt("hello", timeout_seconds=1)
    popen.assert_not_called()


def test_probe_inconclusive_falls_through_to_spawn_error():
    """Missing binary: the probe must NOT mask the established spawn error."""
    client = ACPClient(agent_name="copilot", api_key="k", base_url=marker_base_url("copilot"))
    with patch("agent.acp_client.subprocess.run", side_effect=FileNotFoundError):
        with patch("agent.acp_client.subprocess.Popen", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="Could not start"):
                client._run_prompt("hello", timeout_seconds=1)


def test_probe_result_cached_per_binary_path():
    from agent.acp_client import _acp_supported

    with patch(
        "agent.acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: copilot [--acp]"),
    ) as run_mock:
        assert _acp_supported("copilot", ["--acp"]) is True
        assert _acp_supported("copilot", ["--acp"]) is True
    assert run_mock.call_count == 1


def test_probe_inconclusive_not_cached():
    from agent.acp_client import _acp_supported

    with patch("agent.acp_client.subprocess.run", side_effect=FileNotFoundError) as run_mock:
        assert _acp_supported("copilot", ["--acp"]) is None
        assert _acp_supported("copilot", ["--acp"]) is None
    assert run_mock.call_count == 2  # inconclusive verdicts retry


@pytest.mark.parametrize("agent,args", [
    ("claude", []),                      # claude-agent-acp: dedicated binary
    ("codex", []),                       # codex-acp: dedicated binary
    ("cursor", ["acp"]),                 # cursor-agent acp: subcommand
    ("gemini", ["--experimental-acp"]),  # gemini: its own flag
])
def test_probe_skipped_for_agents_that_never_pass_acp_flag(agent, args):
    """The generalization that keeps plugin agents working.

    Only ``--acp`` launchers are probed. Every other adapter speaks ACP
    through a dedicated binary or its own subcommand/flag, has no ``--acp``
    to advertise in ``--help``, and must not be gated on one.
    """
    from agent.acp_client import _acp_supported

    with patch("agent.acp_client.subprocess.run") as run_mock:
        assert _acp_supported(f"{agent}-acp-bin", args) is True
    run_mock.assert_not_called()


# ── sweeper review 4814814081: MCP forwarding, command shapes, diagnostics ──


def test_normalize_command_joins_argv_lists():
    """A list-shaped rawInput.command must reach approval as a command line.

    str() on a list yields a Python repr, so the approval layer would match
    its rules against brackets and quotes instead of the executable and its
    flags — a deny rule keyed on "rm -rf" would never fire.
    """
    from agent.acp_client import _extract_permission_command, _normalize_command

    assert _normalize_command(["rm", "-rf", "/tmp/x"]) == "rm -rf /tmp/x"
    assert _normalize_command(["echo", "a b"]) == "echo 'a b'"
    assert _normalize_command("rm -rf /tmp/x") == "rm -rf /tmp/x"
    assert _normalize_command(None) == ""
    assert _normalize_command([]) == ""

    command, _ = _extract_permission_command(
        {"rawInput": {"command": ["rm", "-rf", "/tmp/x"]}, "title": "Remove"}
    )
    assert command == "rm -rf /tmp/x"
    assert "[" not in command


def test_error_detail_is_fingerprinted_and_bounded():
    from agent.acp_client import _ERROR_DATA_LIMIT, _error_detail

    assert _error_detail({"message": "boom"}) == ""
    for payload in (
        {"message": "tool X failed: ENOENT"},
        "plain detail",
        {"status": 503},
    ):
        detail = _error_detail({"data": payload})
        assert detail.startswith(" ([REDACTED adapter diagnostic sha256:")
        assert str(payload) not in detail
    long = _error_detail({"data": "x" * 5000})
    assert len(long) < _ERROR_DATA_LIMIT + 40
    assert long.startswith(" ([REDACTED adapter diagnostic sha256:")


def test_adapter_diagnostics_are_redacted_and_bounded():
    from agent.acp_client import (
        _ERROR_DATA_LIMIT,
        _ERROR_MESSAGE_LIMIT,
        _STDERR_LIMIT,
        _error_detail,
        _safe_diagnostic_text,
    )

    secrets = (
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        "opaqueCredentialValue987654321",
        "hunter2",
    )
    detail = _error_detail(
        {"data": {"message": f"upstream token={secrets[1]} password={secrets[2]}"}}
    )
    message = _safe_diagnostic_text(
        f"authentication failed: {secrets[0]}",
        _ERROR_MESSAGE_LIMIT,
    )
    stderr = _safe_diagnostic_text(
        (f"url=https://x.invalid/?token={secrets[1]} password={secrets[2]}\n" * 200),
        _STDERR_LIMIT,
    )

    for rendered in (detail, message, stderr):
        for secret in secrets:
            assert secret not in rendered
        assert "[REDACTED adapter diagnostic sha256:" in rendered
    assert len(detail) < _ERROR_DATA_LIMIT + 40
    assert len(message) < _ERROR_MESSAGE_LIMIT + 40
    assert len(stderr) < _STDERR_LIMIT + 40


def test_adapter_diagnostic_redaction_failure_is_fail_closed():
    from agent.acp_client import _safe_diagnostic_text

    with patch("agent.acp_client.redact_sensitive_text", side_effect=RuntimeError("boom")):
        assert _safe_diagnostic_text("sensitive adapter output", 200) == (
            "[REDACTED - diagnostic unavailable]"
        )


def test_acp_mcp_servers_converts_config_to_acp_shape():
    """Forwarded servers come from the same loader native MCP uses.

    _load_mcp_config already filters exfiltration-shaped entries and resolves
    ${ENV_VAR}; re-reading config.yaml here is how the two paths would drift
    on which servers are safe to spawn.
    """
    from agent.acp_client import _acp_mcp_servers

    config = {
        "stdioserver": {
            "command": "uvx",
            "args": ["some-server"],
            "env": {"TOKEN": "resolved-secret"},
        },
        "httpserver": {
            "url": "https://example.invalid/mcp",
            "headers": {"Authorization": "Bearer t"},
        },
        "brokenserver": {"description": "neither url nor command"},
    }
    with patch("tools.mcp_tool._load_mcp_config", return_value=config):
        servers = _acp_mcp_servers()

    by_name = {s["name"]: s for s in servers}
    assert set(by_name) == {"stdioserver", "httpserver"}, "malformed entry dropped"
    assert by_name["stdioserver"]["command"] == "uvx"
    assert by_name["stdioserver"]["args"] == ["some-server"]
    # ACP takes env/headers as name/value pair lists, not objects.
    assert by_name["stdioserver"]["env"] == [
        {"name": "TOKEN", "value": "resolved-secret"}
    ]
    assert by_name["httpserver"]["type"] == "http"
    assert by_name["httpserver"]["headers"] == [
        {"name": "Authorization", "value": "Bearer t"}
    ]


def test_acp_mcp_servers_returns_empty_when_config_layer_fails():
    """A broken MCP config must not take the whole ACP turn down."""
    from agent.acp_client import _acp_mcp_servers

    with patch("tools.mcp_tool._load_mcp_config", side_effect=RuntimeError("boom")):
        assert _acp_mcp_servers() == []


@pytest.mark.parametrize("name,expected", [
    ("mcp__github__create_issue", "github"),
    ("mcp__my_server__tool__with__underscores", "my_server"),
    ("mcp__nounderscoresuffix", None),
    ("mcp____empty", None),
    ("terminal_tool", None),
    ("", None),
])
def test_mcp_server_from_tool_name(name, expected):
    from agent.acp_client import _mcp_server_from_tool_name

    assert _mcp_server_from_tool_name(name) == expected


def test_mcp_permissions_scoped_to_forwarded_servers():
    """mcp__* is decided by provenance, not by shell-command rules.

    Approving an MCP call because its title happened not to look like a
    dangerous shell command is meaningless. The question that matters is
    whether Hermes handed the agent that server in the first place, so a
    server the agent found on its own is refused even in bridge mode.
    """
    from agent.acp_client import _select_option

    client = _client()
    client._forwarded_mcp_servers = {"github"}

    params = {
        "toolCall": {"toolName": "mcp__github__create_issue"},
        "options": _permission_params()["options"],
    }
    with patch("tools.approval.check_dangerous_command") as gate:
        response = client._decide_permission(1, params)
    assert _outcome(response) == {
        "outcome": "selected",
        "optionId": _select_option(params["options"], allow=True),
    }
    gate.assert_not_called(), "MCP calls must not reach the shell-command gate"

    # A server Hermes never forwarded is refused, however benign it looks.
    params["toolCall"] = {"toolName": "mcp__attacker__exfiltrate"}
    with patch("tools.approval.check_dangerous_command") as gate:
        response = client._decide_permission(1, params)
    assert _outcome(response) == {
        "outcome": "selected",
        "optionId": _select_option(params["options"], allow=False),
    }
    gate.assert_not_called()


def test_mcp_permission_denied_when_nothing_was_forwarded():
    """Empty forwarded set denies every mcp__* call — fail closed."""
    client = _client()
    assert client._forwarded_mcp_servers == set()

    params = {
        "toolCall": {"toolName": "mcp__github__create_issue"},
        "options": _permission_params()["options"],
    }
    from agent.acp_client import _select_option

    with patch("tools.approval.check_dangerous_command") as gate:
        response = client._decide_permission(1, params)
    assert _outcome(response) == {
        "outcome": "selected",
        "optionId": _select_option(params["options"], allow=False),
    }
    gate.assert_not_called()


def test_non_mcp_permissions_still_reach_the_shell_gate():
    """The MCP branch must not swallow ordinary command approvals."""
    client = _client()
    client._forwarded_mcp_servers = {"github"}

    params = {
        "toolCall": {"rawInput": {"command": ["ls", "-la"]}, "title": "List"},
        "options": _permission_params()["options"],
    }
    with patch(
        "tools.approval.check_dangerous_command", return_value={"approved": True}
    ) as gate:
        client._decide_permission(1, params)
    gate.assert_called_once()
    # And it receives a shell string, not a Python list repr.
    assert gate.call_args[0][0] == "ls -la"
