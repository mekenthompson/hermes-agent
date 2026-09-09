"""Behavioral timing contracts using real ACP children without provider credentials."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.copilot_acp_client import CopilotACPClient as Client

CHILD = r'''
import json, sys, time
mode = sys.argv[1]
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if mode == 'delays': time.sleep(0.12)
    result = {'sessionId':'test-session'} if method == 'session/new' else {}
    if method == 'session/prompt':
        if mode == 'stream':
            for kind, text in [('agent_message_chunk','hello'), ('agent_thought_chunk','thinking'), ('agent_message_chunk',' world')]:
                print(json.dumps({'method':'session/update', 'params':{'sessionId':'test-session', 'update':{'sessionUpdate':kind,'content':{'type':'text','text':text}}}}),flush=True)
                time.sleep(0.15)
        elif mode == 'hang': time.sleep(30)
    print(json.dumps({'id':msg.get('id'), 'result':result}), flush=True)
    if mode == 'blocked' and method == 'session/new': time.sleep(30)
'''

class ACPPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.script = Path(self.temp.name)/'child.py'
        self.script.write_text(CHILD, encoding="utf-8")
        self.old_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = self.temp.name
        self.addCleanup(self.restore_home)

    def restore_home(self):
        if self.old_home is None: os.environ.pop('HERMES_HOME',None)
        else: os.environ['HERMES_HOME'] = self.old_home

    def client(self, mode):
        client = Client(acp_command=sys.executable, acp_args=[str(self.script),mode], acp_cwd=self.temp.name)
        self.addCleanup(client.close)
        return client

    def test_blocked_prompt_write_obeys_deadline(self):
        client=self.client('blocked')
        start=time.monotonic()
        with self.assertRaises((TimeoutError, RuntimeError)):
            client.chat.completions.create(messages=[{'role':'user','content':'x'*2_000_000}],timeout=0.2)
        self.assertLess(time.monotonic()-start,1.5)

    def test_setup_stages_share_one_deadline(self):
        client=self.client('delays')
        start=time.monotonic()
        with self.assertRaises(TimeoutError):
            client.chat.completions.create(messages=[{'role':'user','content':'hello'}],timeout=0.2)
        self.assertLess(time.monotonic()-start,0.65)

    def test_stream_exposes_text_and_reasoning_before_completion(self):
        client=self.client('stream')
        start=time.monotonic()
        stream=client.chat.completions.create(messages=[{'role':'user','content':'hello'}],timeout=2,stream=True)
        self.assertLess(time.monotonic()-start,0.15)
        first=next(stream)
        self.assertEqual(first.choices[0].delta.content,'hello')
        self.assertLess(time.monotonic()-start,0.35)
        chunks=[first,*stream]
        self.assertEqual(''.join(c.choices[0].delta.content or '' for c in chunks if c.choices),'hello world')
        self.assertEqual(''.join(c.choices[0].delta.reasoning_content or '' for c in chunks if c.choices),'thinking')
        self.assertEqual(chunks[-2].choices[0].finish_reason,'stop')

    def test_close_interrupts_blocked_write(self):
        client=self.client('blocked')
        errors=[]
        def invoke():
            try: client.chat.completions.create(messages=[{'role':'user','content':'x'*2_000_000}],timeout=30)
            except Exception as exc: errors.append(exc)
        thread=threading.Thread(target=invoke,daemon=True);thread.start()
        time.sleep(0.15)
        start=time.monotonic();client.close();thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic()-start,1.5)
        self.assertTrue(errors)

    def test_stream_close_cancels_producer(self):
        client=self.client('hang')
        stream=client.chat.completions.create(messages=[{'role':'user','content':'hello'}],timeout=30,stream=True)
        time.sleep(0.1);start=time.monotonic();stream.close()
        self.assertLess(time.monotonic()-start,1.5)
        self.assertFalse(stream._worker.is_alive())

    def test_lifecycle_abort_stops_real_transport_and_poisoned_slot_is_not_reused(self):
        from agent.client_lifecycle import ClientLifecycleMixin
        owner=ClientLifecycleMixin()
        client=self.client('blocked')
        owner._store_request_slot('_request_client_cache',client,{'route':'acp'})
        errors=[]
        def invoke():
            try: client.chat.completions.create(messages=[{'role':'user','content':'x'*2_000_000}],timeout=30)
            except Exception as exc: errors.append(exc)
        thread=threading.Thread(target=invoke,daemon=True);thread.start()
        time.sleep(0.15)
        owner._abort_request_openai_client(client,reason='stale_stream')
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertFalse(owner._release_request_slot('_request_client_cache',client,'stream_request_complete'))
        self.assertEqual(owner._checkout_request_slot('_request_client_cache',{'route':'acp'}),(None,None))

    def test_tool_syntax_split_across_chunks_is_not_exposed_as_prose(self):
        text='<tool_call>{"id":"test","type":"function","function":{"name":"read_file","arguments":"{}"}}</tool_call>'
        self.script.write_text(CHILD.replace("[('agent_message_chunk','hello'), ('agent_thought_chunk','thinking'), ('agent_message_chunk',' world')]",repr([('agent_message_chunk',text[:3]),('agent_message_chunk',text[3:])])) , encoding="utf-8")
        client=self.client('stream')
        chunks=list(client.chat.completions.create(messages=[{'role':'user','content':'hello'}],timeout=2,stream=True))
        self.assertFalse(''.join(c.choices[0].delta.content or '' for c in chunks if c.choices))
        calls=[call for c in chunks if c.choices for call in (c.choices[0].delta.tool_calls or [])]
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].function.name,'read_file')

    def test_http_timeout_does_not_turn_connect_budget_into_generation_limit(self):
        from types import SimpleNamespace
        helper=Client._create.__globals__['_timeout'] if hasattr(Client,'_create') else Client._create_chat_completion.__globals__['_effective_timeout']
        self.assertEqual(helper(SimpleNamespace(read=120,write=1800,connect=30,pool=30)),120)
        self.assertEqual(helper(0.25),0.25)

    def test_stream_worker_inherits_profile_context_for_child_environment(self):
        import contextvars
        from unittest.mock import patch
        scope=contextvars.ContextVar('test_profile_scope',default='missing')
        globals_=Client._spawn.__globals__
        key='build_subprocess_env' if 'build_subprocess_env' in globals_ else '_build_subprocess_env'
        original=globals_[key]
        def environment():
            env=original();env['ACP_TEST_SCOPE']=scope.get();return env
        self.script.write_text(CHILD.replace('import json, sys, time','import json, sys, time, os').replace("('agent_message_chunk','hello')","('agent_message_chunk',os.environ['ACP_TEST_SCOPE'])"), encoding="utf-8")
        client=self.client('stream')
        token=scope.set('profile-a')
        try:
            with patch.dict(globals_,{key:environment}):
                stream=client.chat.completions.create(messages=[{'role':'user','content':'hello'}],timeout=2,stream=True)
                scope.set('profile-b')
                self.assertEqual(next(stream).choices[0].delta.content,'profile-a')
                list(stream)
        finally:
            scope.reset(token)

    def test_abandoned_flood_stream_times_out_and_releases_pumps(self):
        self.script.write_text(CHILD.replace("[('agent_message_chunk','hello'), ('agent_thought_chunk','thinking'), ('agent_message_chunk',' world')]","[('agent_message_chunk','x')]*1000").replace('time.sleep(0.15)','pass'), encoding="utf-8")
        client=self.client('stream')
        before={t.ident for t in threading.enumerate() if t.name.endswith('acp-pump')}
        stream=client.chat.completions.create(messages=[{'role':'user','content':'hello'}],timeout=0.3,stream=True)
        stream._worker.join(1.5)
        self.assertFalse(stream._worker.is_alive())
        after={t.ident for t in threading.enumerate() if t.name.endswith('acp-pump')}
        self.assertEqual(after,before)
        stream.close()

    @unittest.skipUnless(os.name == 'posix','POSIX inherited-pipe reproducer')
    def test_help_probe_timeout_does_not_drain_descendant_pipes(self):
        from agent.copilot_acp_client import _acp_supported
        self.script.write_text('#!'+sys.executable+'\nimport subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])\ntime.sleep(30)\n', encoding="utf-8")
        self.script.chmod(0o700)
        start=time.monotonic()
        self.assertIsNone(_acp_supported(str(self.script),['--acp'],timeout=0.15))
        self.assertLess(time.monotonic()-start,0.8)

    def test_ordinary_code_braces_resume_visible_streaming(self):
        fragments=[('agent_message_chunk','  def f():\n    return {'),('agent_message_chunk',"'ok': True}  ")]
        self.script.write_text(CHILD.replace("[('agent_message_chunk','hello'), ('agent_thought_chunk','thinking'), ('agent_message_chunk',' world')]",repr(fragments)), encoding="utf-8")
        client=self.client('stream')
        stream=client.chat.completions.create(messages=[{'role':'user','content':'hello'}],timeout=2,stream=True)
        first=next(stream);second=next(stream)
        self.assertIsNone(second.choices[0].finish_reason)
        self.assertEqual(first.choices[0].delta.content+second.choices[0].delta.content,"def f():\n    return {'ok': True}")
        self.assertFalse(''.join(c.choices[0].delta.content or '' for c in stream if c.choices))

    def test_final_chunk_is_not_lost_when_completion_races_queue_timeout(self):
        import queue
        create=Client._create if hasattr(Client,'_create') else Client._create_chat_completion
        stream_type=create.__globals__['LiveStream']
        stream=stream_type.__new__(stream_type)
        stream._stopped=threading.Event()
        stream._finished=threading.Event()
        stream._error=None
        sentinel=object()
        class RacingQueue(queue.Queue):
            raced=False
            def get(self, *args, **kwargs):
                if not self.raced:
                    self.raced=True
                    self.put(sentinel)
                    stream._finished.set()
                    raise queue.Empty
                return super().get(*args, **kwargs)
        stream._queue=RacingQueue()
        self.assertIs(next(stream),sentinel)
        with self.assertRaises(StopIteration): next(stream)
