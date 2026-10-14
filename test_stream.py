import json
import sys
import unittest
from unittest.mock import patch,Mock
import bridge
from cli_stream import Observer,capture_directory


class StreamTests(unittest.TestCase):
    def test_only_whitelist_fields_leave_observer(self):
        emitted=[]; observer=Observer(emitted.append)
        raw=[{'type':'system','subtype':'init','cwd':'private-path','model':'private-key'},
             {'type':'system','subtype':'api_retry','error':'rate_limit','attempt':2,'error_status':429,'message':'private-key'},
             {'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'private-key'}}},
             {'type':'result','result':'answer','is_error':False}]
        for item in raw: observer.feed((json.dumps(item)+'\n').encode())
        observer.finish(); summary=observer.summary()
        self.assertNotIn('private',json.dumps(summary)); self.assertNotIn('private',json.dumps(emitted))
        self.assertTrue(summary['initialized']); self.assertTrue(summary['final_result_received'])
        self.assertEqual(summary['events'][1]['error_status'],429)

    def test_fragmented_utf8_and_bounded_events(self):
        observer=Observer(); encoded=(json.dumps({'type':'result','result':'中文'},ensure_ascii=False)+'\n').encode()
        for byte in encoded: observer.feed(bytes([byte]))
        self.assertEqual(observer.result['result'],'中文')
        for _ in range(130): observer.consume({'type':'system','subtype':'api_retry','error':{'secret':'hidden'}})
        self.assertEqual(len(observer.events),120); self.assertGreater(observer.dropped,0)

    def test_capture_limits_and_invalid_json_fail_closed(self):
        with self.assertRaises(ValueError): Observer().feed(b'x'*250001)
        with self.assertRaises(ValueError): Observer().feed(b'not-json\n')
        observer=Observer(); observer.total_bytes=2_000_000
        with self.assertRaises(ValueError): observer.feed(b'\n')

    def test_live_file_observation_preserves_writer_offset(self):
        code='import json,time; print(json.dumps({"type":"system","subtype":"init"}),flush=True); time.sleep(.6); print(json.dumps({"type":"result","result":"answer"}),flush=True)'
        events=[]
        with patch.object(bridge,'executable',return_value=sys.executable):
            status,text,_=bridge.run_cli(['-c',code,'--verbose','stream-json'],timeout=3,on_event=events.append)
        self.assertEqual(status,0); self.assertEqual(json.loads(text)['result'],'answer')
        self.assertEqual([e['kind'] for e in events],['initialized','final_result'])

    def test_timeout_retains_filtered_diagnostics(self):
        code='import json,time; print(json.dumps({"type":"system","subtype":"init","secret":"hidden"}),flush=True); time.sleep(30)'
        with patch.object(bridge,'executable',return_value=sys.executable):
            with self.assertRaises(TimeoutError) as caught:
                bridge.run_cli(['-c',code,'--verbose','stream-json'],timeout=.8)
        self.assertTrue(caught.exception.stream_diagnostics['initialized'])
        self.assertNotIn('hidden',json.dumps(caught.exception.stream_diagnostics))

    def test_persistent_cleanup_lock_does_not_replace_original_timeout(self):
        directory=Mock(name='fake-directory'); directory.cleanup.side_effect=PermissionError('locked')
        with patch('cli_stream.tempfile.TemporaryDirectory',return_value=directory),patch('cli_stream.time.sleep'):
            with self.assertRaises(TimeoutError) as caught:
                with capture_directory(): raise TimeoutError('original deadline')
        self.assertEqual(str(caught.exception),'original deadline'); self.assertTrue(caught.exception.capture_cleanup_failed)

    def test_transport_keepalive_is_not_model_output(self):
        observer=Observer()
        observer.consume({'type':'stream_event','event':{'type':'ping'}})
        observer.consume({'type':'stream_event','event':{'type':'message_start'}})
        observer.consume({'type':'stream_event','event':{'type':'unknown','delta':{'secret':'hidden'}}})
        self.assertEqual(observer.counts.get('output_events',0),0)
        self.assertEqual(observer.counts['transport_keepalive_events'],1)


if __name__=='__main__': unittest.main()
