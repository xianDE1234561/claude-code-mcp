import queue
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from mcp_client import Client


class ClientTests(unittest.TestCase):
    def fake_server_config(self,script):
        root=Path(__file__).resolve().parent/'test-artifacts'
        root.mkdir(exist_ok=True)
        temporary=tempfile.TemporaryDirectory(prefix='client-',dir=root)
        folder=Path(temporary.name); assert folder.resolve().is_relative_to(root.resolve())
        server=folder/'server.py'; server.write_text(script,encoding='utf-8')
        config=folder/'config.toml'
        config.write_text(f"[mcp_servers.claude_cli]\ncommand = '{sys.executable}'\nargs = ['{server}']\n",encoding='utf-8')
        return temporary,config

    def test_total_timeout_sends_cancellation(self):
        client=Client.__new__(Client)
        client.next_id=0; client.messages=queue.Queue(); client.send=Mock(); client.notify=Mock()
        client.messages.put({'jsonrpc':'2.0','method':'notifications/progress'})
        with self.assertRaises(TimeoutError): client.request('tools/call',{},timeout=.02)
        client.notify.assert_called_once_with('notifications/cancelled',{'requestId':1})

    def test_provider_limit_controls_client_deadline(self):
        client=Client.__new__(Client); client.request=Mock(return_value={})
        client.call('review_diff',{'timeout_seconds':60})
        self.assertEqual(client.request.call_args.args[2],75)

    def test_initialization_failure_reaps_started_process(self):
        temporary,config=self.fake_server_config('import time\ntime.sleep(30)\n')
        started=[]; original=subprocess.Popen
        def track(*args,**kwargs):
            process=original(*args,**kwargs); started.append(process); return process
        try:
            with patch('mcp_client.subprocess.Popen',side_effect=track):
                with self.assertRaises(TimeoutError): Client(config_path=config,startup_timeout=.05)
            self.assertEqual(len(started),1); self.assertIsNotNone(started[0].poll())
        finally: temporary.cleanup()

    def test_stderr_is_drained_and_bounded(self):
        script='''import sys,json
sys.stderr.write("x"*65536); sys.stderr.flush()
for line in sys.stdin:
    m=json.loads(line)
    if "id" in m:
        print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{}}),flush=True)
'''
        temporary,config=self.fake_server_config(script)
        client=None
        try:
            client=Client(config_path=config,startup_timeout=3)
            self.assertLessEqual(len(client.stderr_tail),8192)
            self.assertTrue(client.stderr_tail)
        finally:
            if client: client.close(); self.assertIsNotNone(client.process.poll())
            temporary.cleanup()

    def test_progress_token_is_requested_and_notifications_delivered(self):
        client=Client.__new__(Client)
        client.next_id=0; client.messages=queue.Queue(); client.send=Mock(); client.notify=Mock()
        client.progress_events=[]; client.on_progress=Mock()
        update={'progressToken':1,'progress':1,'message':'collecting'}
        client.messages.put({'method':'notifications/progress','params':update})
        client.messages.put({'id':1,'result':{'ok':True}})
        self.assertEqual(client.request('tools/call',{}),{'ok':True})
        self.assertEqual(client.send.call_args.args[0]['params']['_meta']['progressToken'],1)
        client.on_progress.assert_called_once_with(update)

    def test_live_status_has_enough_client_grace(self):
        client=Client.__new__(Client); client.request=Mock(return_value={})
        client.call('claude_status',{'live_probe':True})
        self.assertEqual(client.request.call_args.args[2],135)


if __name__=='__main__': unittest.main()
