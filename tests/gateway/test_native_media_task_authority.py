"""Authenticated native media retains its task authority without a caption ritual."""
import asyncio
import base64
import json
import pytest

from tests.gateway.test_plugin_command_task_authority import _discord_runtime
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('media_kind', ['photo', 'voice'])
@pytest.mark.parametrize('caption', ['Analyze the attached image', ''])
async def test_authenticated_media_tools(tmp_path, monkeypatch, caption, media_kind):
    from gateway.platforms.event import MessageEvent, MessageType
    from model_tools import handle_function_call
    from tools.approval_context import current_approval_task_context, approval_task_revoked
    runtime = await _discord_runtime(tmp_path, monkeypatch)
    image = tmp_path / ('diagram.png' if media_kind == 'photo' else 'instruction.ogg')
    image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMo0AAAAASUVORK5CYII=') if media_kind == 'photo' else b'fixture voice')
    from tools import vision_tools
    async def vision(**kwargs):
        return json.dumps({'success': True, 'analysis': 'A diagram supplied by the authenticated user'})
    monkeypatch.setattr(vision_tools, 'vision_analyze_tool', vision)
    from tools import transcription_tools
    monkeypatch.setattr(transcription_tools, 'transcribe_audio', lambda *a, **kw:
        {'success': True, 'transcript': 'Read the instructions file for my existing task'})
    readable = tmp_path / 'instructions.txt'
    readable.write_text('use the uploaded image')
    seen = []

    async def model(**kwargs):
        origin = current_approval_task_context()
        reply = json.loads(await asyncio.to_thread(handle_function_call, 'read_file',
            {'path': str(readable)}, task_id='photo-analysis'))
        seen.append((kwargs['message'], origin, approval_task_revoked(), reply))
        return {'final_response': 'done', 'messages': []}

    monkeypatch.setattr(runtime.runner, '_run_agent', model)
    try:
        await runtime.adapter.handle_message(MessageEvent(text=caption, source=runtime.source,
            message_id='photo', message_type=MessageType.PHOTO if media_kind == 'photo' else MessageType.VOICE,
            media_urls=[str(image)], media_types=['image/png' if media_kind == 'photo' else 'audio/ogg']))
        await async_wait_until(lambda: bool(seen), timeout=15, what='authenticated photo turn')
        assert seen[0][1] is not None and not seen[0][2], json.dumps(seen, default=str)
        assert seen[0][1].identity.startswith('messaging:')
        assert not seen[0][3].get('error'), json.dumps(seen, default=str)
        assert 'use the uploaded image' in seen[0][3]['content']
    finally:
        await asyncio.wait_for(asyncio.gather(*(live.task for live in runtime.authority.sessions.values()
            if live.task is not None)), 15)
        await asyncio.to_thread(runtime.registry.kill_all, source='test-cleanup')
        runtime.db.close()
