import json
import sys
import threading
import time
import unittest
from unittest.mock import patch
import bridge


class BridgeTests(unittest.TestCase):
    def test_validation_prevents_launch(self):
        for args in ({'prompt':''},{'prompt':'x','timeout_seconds':True},
                     {'prompt':'x','model':'--bad'}, {'prompt':'x','unknown':1},
                     {'prompt':'x','context':42}, {'prompt':'x'*100001}):
            with self.subTest(args=str(args)[:50]), patch.object(bridge,'run_cli') as run:
                self.assertTrue(bridge.ask(args)['isError']); run.assert_not_called()

    def test_prompt_never_becomes_command_argument(self):
        prompt='中文 " $() & | %PATH% ` ;\nsecond line'
        fake=(0,json.dumps({'result':'好','modelUsage':{'test-model':{}}}), '')
        with patch.object(bridge,'run_cli',return_value=fake) as run:
            response=bridge.ask({'prompt':prompt,'context':'context','model':'test-model'})
        self.assertFalse(response['isError'])
        args,text,_,_=run.call_args.args
        self.assertNotIn(prompt,args); self.assertIn(prompt,text)
        self.assertEqual(args[args.index('--tools')+1],'')
        self.assertIn('--safe-mode',args); self.assertIn('--strict-mcp-config',args)
        self.assertIn('--no-session-persistence',args)

    def test_provider_error_is_not_success(self):
        with patch.object(bridge,'run_cli',return_value=(0,'{"is_error":true,"result":"HTTP 401"}','')):
            self.assertTrue(bridge.ask({'prompt':'x'})['isError'])

    def test_live_probe_requires_exact_answer(self):
        with patch.object(bridge,'run_cli',side_effect=[(0,'v1',''),(0,'{}','')]), \
             patch.object(bridge,'executable',return_value='fake.exe'), \
             patch.object(bridge,'ask',return_value=bridge.result('{"answer":"wrong"}')):
            self.assertTrue(bridge.status({'live_probe':True})['isError'])

    def test_credential_redaction(self):
        with patch.object(bridge,'settings_env',return_value={'ANTHROPIC_AUTH_TOKEN':'private-secret-test'}):
            self.assertNotIn('private-secret-test',bridge.result('error private-secret-test')['content'][0]['text'])

    def test_timeout_terminates_child(self):
        with patch.object(bridge,'executable',return_value=sys.executable):
            with self.assertRaises(TimeoutError):
                bridge.run_cli(['-c','import time; time.sleep(30)'],timeout=.2)

    def test_cancel_terminates_child(self):
        event=threading.Event(); event.set()
        with patch.object(bridge,'executable',return_value=sys.executable):
            with self.assertRaisesRegex(RuntimeError,'cancelled'):
                bridge.run_cli(['-c','import time; time.sleep(30)'],cancelled=event)

    def test_inherited_output_handles_do_not_block_timeout(self):
        code='import subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],stdout=sys.stdout,stderr=sys.stderr); time.sleep(30)'
        started=time.monotonic()
        with patch.object(bridge,'executable',return_value=sys.executable):
            with self.assertRaises(TimeoutError): bridge.run_cli(['-c',code],timeout=.5)
        self.assertLess(time.monotonic()-started,3)

if __name__=='__main__': unittest.main()
