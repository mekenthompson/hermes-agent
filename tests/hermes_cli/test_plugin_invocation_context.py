"""Plugin tool invocation-context integration tests."""

import json


def test_plugin_opt_in_receives_host_context_not_dispatch_keyword(tmp_path):
    """The plugin registrar reaches real registry dispatch without caller-supplied identity."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from tools.registry import ToolInvocationContext, registry

    manager = PluginManager(scope_key=str(tmp_path))
    context = PluginContext(PluginManifest(name="context-plugin", key="context-plugin"), manager)
    tool_name = "plugin_context_probe"
    seen = []

    def handler(args, *, invocation_context):
        seen.append((args, invocation_context))
        return json.dumps({"platform": invocation_context.platform})

    context.register_tool(
        name=tool_name,
        toolset="plugin-context-test",
        schema={"name": tool_name, "description": "", "parameters": {"type": "object"}},
        handler=handler,
        inject_invocation_context=True,
    )
    tokens = set_session_vars(platform="discord", user_id="host-user", session_id="host-session")
    try:
        result = registry.dispatch(
            tool_name,
            {"user_id": "model-chosen"},
            scope=manager.scope_key,
            invocation_context="spoofed",
        )
    finally:
        clear_session_vars(tokens)
        registry.deregister(tool_name, scope=manager.scope_key)

    assert json.loads(result) == {"platform": "discord"}
    assert seen[0][0] == {"user_id": "model-chosen"}
    assert isinstance(seen[0][1], ToolInvocationContext)
    assert seen[0][1].user_id == "host-user"
    assert seen[0][1].session_id == "host-session"
