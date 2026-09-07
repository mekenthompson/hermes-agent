# Internal plugin execution lifecycle v1

`GatewayRunner.dispatch_internal_plugin_event(event, execution_id=...)` and
`request_stop(session_key, expected_execution_id, reason=...)` retain the PR23
delivery ABI. A receipt `status: accepted` means...[truncated]