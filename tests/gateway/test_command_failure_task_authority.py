"""Command children keep authority after success and lose it after native failure/cancellation."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shlex
import signal
import threading

import pytest

from tests.waiting import async_wait_until, wait_until


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_discord_command_completion_obeys_origin_outcome(tmp_path, monkeypatch, outcome):
    from gateway.platforms.event import MessageEvent
    from model_tools import handle_function_call
    from tests.gateway.test_plugin_command_task_authority import _discord_runtime

    runtime = await _discord_runtime(tmp_path, monkeypatch)
    entry = runtime.runner.session_store.lookup_by_session_key(runtime.key)
    if runtime.db.get_session(entry.session_id) is None:
        runtime.db.create_session(entry.session_id, "discord")
    release, effect = tmp_path / "release", tmp_path / "completion-effect"
    receipts, writes = [], []
    finished = threading.Event()

    def handler(arg):
        try:
            receipts.append(json.loads(runtime.context.dispatch_tool("terminal", {
                "command": f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.05; done; printf finished",
                "background": True, "notify": True})))
            if outcome == "failure":
                raise RuntimeError("command failed after starting its child")
            if outcome == "cancelled":
                raise asyncio.CancelledError("command cancelled after starting its child")
            return "child started"
        finally:
            finished.set()

    async def model(**_kwargs):
        writes.append(json.loads(await asyncio.to_thread(handle_function_call, "write_file",
            {"path": str(effect), "content": "command completion"}, task_id="command-completion")))
        return {"final_response": "completion handled", "messages": []}

    registration = runtime.context.register_command("authority-outcome", handler)
    monkeypatch.setattr(runtime.runner, "_run_agent", model)
    try:
        await runtime.adapter.handle_message(MessageEvent(text="/authority-outcome", source=runtime.source, message_id="command"))
        assert await asyncio.to_thread(finished.wait, 10)
        await async_wait_until(lambda: not runtime.adapter._background_tasks, timeout=10, what="native command settlement")
        assert receipts and not receipts[0].get("error"), receipts
        process = runtime.registry.get(receipts[0]["session_id"])
        assert process.approval_task_reservation is not None
        release.touch()
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        watcher = next(value for value in runtime.registry.pending_watchers if value["session_id"] == process.id)
        event = runtime.runner._build_process_completion_event(watcher, process, process.id)
        assert await runtime.runner._deliver_completion_notification("finished", event)
        await async_wait_until(lambda: bool(writes), timeout=10, what="native completion consumer")
        if outcome == "success":
            assert effect.read_text() == "command completion", writes
        else:
            assert writes[0].get("code") == "task_authority_revoked", writes
            assert not effect.exists()
    finally:
        release.touch()
        registration.dispose()
        await asyncio.to_thread(runtime.registry.kill_all, source="test-cleanup")
        runtime.db.close()


class _CompletionModel(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body.get("messages") or []
        tool = next((message for message in reversed(messages) if message.get("role") == "tool"), None)
        if tool is not None:
            self.server.results.append(tool["content"])
            message = {"role": "assistant", "content": "completion handled"}
        elif messages:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "completion-write", "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps({"path": str(self.server.effect), "content": "native completion"})}}]}
        else:
            message = {"role": "assistant", "content": "ready"}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        if body.get("stream"):
            delta = dict(message)
            if delta.get("tool_calls"):
                delta["tool_calls"] = [dict(call, index=index) for index, call in enumerate(delta["tool_calls"])]
            payload = ("data: " + json.dumps({"id": "completion", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
                       + "\n\ndata: [DONE]\n\n").encode()
            kind = "text/event-stream"
        else:
            payload = json.dumps({"id": "completion", "choices": [{"index": 0, "message": message, "finish_reason": finish}]}).encode()
            kind = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_websocket_plugin_command_completion_obeys_origin_outcome(tmp_path, outcome):
    from tests.gateway.fixtures.local_recovery_probe import daemon
    from tests.gateway.test_cli_plugin_command_in_gateway import _plugin_home, _Wire

    release, receipt, effect = tmp_path / "release", tmp_path / "receipt.json", tmp_path / "completion-effect"
    command = f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.05; done; printf finished"
    plugin = f'''import json
from pathlib import Path

def register(ctx):
    def command(arg):
        reply = ctx.dispatch_tool("terminal", {{"command": {command!r}, "background": True, "notify": True}})
        Path({str(receipt)!r}).write_text(reply)
        if {outcome!r} == "failure":
            raise RuntimeError("command failed after starting its child")
        return "child started"
    ctx.register_command("authority-outcome", command)
'''
    peer = ThreadingHTTPServer(("127.0.0.1", 0), _CompletionModel)
    peer.effect, peer.results = effect, []
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    try:
        with _plugin_home(tmp_path, plugin) as (root, home, env):
            config = json.loads((home / "config.yaml").read_text())
            config["model"]["base_url"] = env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{peer.server_port}/v1"
            config["platform_toolsets"]["cli"] = ["terminal", "file"]
            (home / "config.yaml").write_text(json.dumps(config))
            with daemon(root, home, env, barrier=False) as (process, desc):
                wire = _Wire(home, desc)
                try:
                    created = wire.call("session.create", request_id="command-outcome", source="cli", cwd=str(tmp_path),
                                        toolsets=["terminal", "file"])
                    sid = created["session_id"]
                    wire.call("session.resume", session_id=sid)
                    result = wire.call("slash.exec", session_id=sid, command="/authority-outcome")
                    assert "failed after starting" in result["output"] if outcome == "failure" else "child started" in result["output"]
                    started = json.loads(receipt.read_text())
                    assert started.get("session_id") and not started.get("error"), started
                    release.touch()
                    wait_until(lambda: bool(peer.results), timeout=30, what="actual websocket command completion tool")
                    tool_result = json.loads(peer.results[0])
                    if outcome == "success":
                        assert effect.read_text() == "native completion", tool_result
                    else:
                        assert tool_result.get("code") == "task_authority_revoked", tool_result
                        assert not effect.exists()
                finally:
                    release.touch()
                    wire.close()
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=20)
    finally:
        release.touch()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
