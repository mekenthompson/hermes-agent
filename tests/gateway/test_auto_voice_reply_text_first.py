"""Auto voice replies deliver the reply text BEFORE synthesizing/uploading audio.

With ``voice.auto_tts`` every reply used to wait for TTS synthesis plus the audio upload
(7-22 s on Slack); the text must never wait on either, and a TTS failure must never block,
drop or duplicate it.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _Adapter(BasePlatformAdapter):
    def __init__(self, platform, log):
        super().__init__(PlatformConfig(enabled=True, token="t"), platform)
        self.log = log

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.log.append(("text", content))
        return SendResult(success=True, message_id="1")

    async def send_voice(self, chat_id, audio_path, reply_to=None, metadata=None, **_kw):
        self.log.append(("upload", audio_path))
        return SendResult(success=True, message_id="2")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


async def _hold_typing(*_a, **_k):
    await asyncio.Event().wait()  # stand-in typing loop; cancelled by the adapter


def _event(platform, message_type=MessageType.TEXT):
    return MessageEvent(
        text="hello", message_type=message_type, message_id="m1",
        source=SessionSource(platform=platform, chat_id="C1", chat_type="dm"),
    )


def _fake_tts(log, fail=False):
    def tts(*, text, output_path=None, **_kw):
        log.append(("synth", text))
        if fail:
            raise RuntimeError("kokoro down")
        with open(output_path, "wb") as fh:
            fh.write(b"\x00" * 16)
        return json.dumps({"success": True, "file_path": output_path})
    return tts


def _runner(adapter, platform):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    return runner


async def _gateway_turn(adapter, platform, runner, response="reply text"):
    """Mimic the real flow: runner decides (returns text + deferred voice), adapter delivers."""
    event = _event(platform)
    adapter._should_auto_tts_for_chat = lambda _c: True
    adapter._keep_typing = _hold_typing

    async def handler(ev):
        # Real runner delivery decision; the adapter sends whatever text it returns.
        return await runner._hmwa_deliver_turn_response(
            ev, ev.source, None, build_session_key(ev.source), 0,
            {}, [{"role": "user", "content": "hello"}], response, "", False)

    adapter.set_message_handler(handler)
    await adapter._process_message_background(event, build_session_key(event.source))


@pytest.mark.asyncio
async def test_runner_voice_reply_sends_text_before_synthesis_and_upload(tmp_path):
    log = []
    adapter = _Adapter(Platform.SLACK, log)
    runner = _runner(adapter, Platform.SLACK)
    with patch("tools.tts_tool.text_to_speech_tool", _fake_tts(log)):
        await _gateway_turn(adapter, Platform.SLACK, runner)
    kinds = [k for k, _ in log]
    assert kinds == ["text", "synth", "upload"], kinds


@pytest.mark.asyncio
async def test_runner_tts_failure_still_delivers_text_exactly_once():
    log = []
    adapter = _Adapter(Platform.SLACK, log)
    runner = _runner(adapter, Platform.SLACK)
    with patch("tools.tts_tool.text_to_speech_tool", _fake_tts(log, fail=True)):
        await _gateway_turn(adapter, Platform.SLACK, runner)
    assert [c for k, c in log if k == "text"] == ["reply text"]
    assert not any(k == "upload" for k, _ in log)


@pytest.mark.asyncio
async def test_runner_upload_failure_still_delivers_text_exactly_once():
    log = []
    adapter = _Adapter(Platform.SLACK, log)
    adapter.send_voice = AsyncMock(side_effect=RuntimeError("upload boom"))
    runner = _runner(adapter, Platform.SLACK)
    with patch("tools.tts_tool.text_to_speech_tool", _fake_tts(log)):
        await _gateway_turn(adapter, Platform.SLACK, runner)
    assert [c for k, c in log if k == "text"] == ["reply text"]


@pytest.mark.asyncio
async def test_base_voice_input_non_telegram_sends_text_before_audio():
    log = []
    adapter = _Adapter(Platform.SLACK, log)
    adapter._should_auto_tts_for_chat = lambda _c: True
    adapter._keep_typing = _hold_typing

    async def play_tts(chat_id, audio_path, caption=None, metadata=None, **_kw):
        log.append(("upload", audio_path))
        return SendResult(success=True, message_id="2")

    adapter.play_tts = play_tts
    adapter.set_message_handler(lambda _e: asyncio.sleep(0, result="reply text"))
    event = _event(Platform.SLACK, MessageType.VOICE)
    with patch("tools.tts_tool.check_tts_requirements", return_value=True), patch(
        "tools.tts_tool.text_to_speech_tool", _fake_tts(log)
    ):
        await adapter._process_message_background(event, build_session_key(event.source))
    assert [k for k, _ in log] == ["text", "synth", "upload"]


@pytest.mark.asyncio
async def test_base_voice_input_tts_failure_delivers_text_once():
    log = []
    adapter = _Adapter(Platform.SLACK, log)
    adapter._should_auto_tts_for_chat = lambda _c: True
    adapter._keep_typing = _hold_typing
    adapter.play_tts = AsyncMock(side_effect=RuntimeError("upload boom"))
    adapter.set_message_handler(lambda _e: asyncio.sleep(0, result="reply text"))
    event = _event(Platform.SLACK, MessageType.VOICE)
    with patch("tools.tts_tool.check_tts_requirements", return_value=True), patch(
        "tools.tts_tool.text_to_speech_tool", _fake_tts(log)
    ):
        await adapter._process_message_background(event, build_session_key(event.source))
    assert [c for k, c in log if k == "text"] == ["reply text"]
