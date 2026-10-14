import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import bridge
import chunk_review as chunks
import context_bundle as context
from test_workflow import report,finding
from volatile_cache import REVIEW_CACHE


class ChunkTests(unittest.TestCase):
    def setUp(self):
        parent=context.ROOT/'test-artifacts'; parent.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent); self.root=Path(self.temp.name).resolve()
        self.env=patch.dict(os.environ,{'CLAUDE_MCP_ALLOWED_ROOTS':json.dumps([str(self.root)])}); self.env.start(); REVIEW_CACHE.clear()
        (self.root/'calc.py').write_text('def mean(xs):\n    return sum(xs) / len(xs)\n',encoding='utf-8')
        (self.root/'other.py').write_text('def identity(x):\n    return x\n',encoding='utf-8')

    def tearDown(self): self.env.stop(); self.temp.cleanup()

    def args(self,**extra):
        return {'project_root':str(self.root),'scope':'files','files':['calc.py','other.py'],
                'include_related':False,**extra}

    def test_multiple_files_keep_exact_locations_and_usage(self):
        with patch.object(bridge,'invoke',side_effect=[(report([finding()]),{'usage':{'input_tokens':10}}),(report(),{'usage':{'input_tokens':7}})]) as invoked:
            result=bridge.workflow('review_diff',self.args())['structuredContent']
        self.assertEqual(invoked.call_count,2); self.assertEqual(result['request']['usage']['input_tokens'],17)
        self.assertEqual(result['chunks']['completed'],2); self.assertTrue(result['chunks']['complete'])
        self.assertEqual(result['findings'][0]['file'],'calc.py'); self.assertEqual(result['findings'][0]['line_start'],2)

    def test_global_timeout_keeps_finished_chunk_and_pending_scope(self):
        with patch.object(bridge,'invoke',side_effect=[(report([finding()]),{}),TimeoutError('model budget ended')]):
            response=bridge.workflow('review_diff',self.args())
        result=response['structuredContent']
        self.assertFalse(response['isError']); self.assertFalse(result['chunks']['complete'])
        self.assertEqual(result['verification_status'],'partial'); self.assertEqual(result['chunks']['completed'],1)
        self.assertEqual(result['chunks']['unreviewed'][0]['focus']['path'],'other.py'); self.assertFalse(result['coverage']['complete'])
        self.assertTrue(result['findings'])

    def test_evidence_from_another_chunk_is_rejected(self):
        with patch.object(bridge,'invoke',side_effect=[(report(),{}),(report([finding()]),{})]):
            result=bridge.workflow('review_diff',self.args())['structuredContent']
        self.assertFalse(result['findings']); self.assertTrue(result['rejected_findings']); self.assertEqual(result['verdict'],'insufficient_context')

    def test_max_chunks_reports_unreviewed_source(self):
        with patch.object(bridge,'invoke',return_value=(report(),{})) as invoked:
            result=bridge.workflow('review_diff',self.args(max_chunks=1))['structuredContent']
        self.assertEqual(invoked.call_count,1); self.assertFalse(result['chunks']['complete'])
        self.assertEqual(result['chunks']['unreviewed'][0]['path'],'other.py')

    def test_large_module_windows_preserve_every_supplied_line(self):
        (self.root/'large.py').write_text('\n'.join(f'value_{i} = {i}' for i in range(700)),encoding='utf-8')
        with context.call_budget(10,wide_context=True):
            bundle=context.collect_review(self.args(files=['large.py']))
        plan=chunks.plan(bundle,chunk_chars=18000)
        seen=[n for chunk in plan['chunks'] for n,_ in chunks.numbers(chunk['snapshots'][0])]
        self.assertEqual(seen,list(range(1,701))); self.assertFalse(plan['unplanned'])
        self.assertTrue(all(len(json.dumps(chunk,ensure_ascii=False))<=18000 for chunk in plan['chunks']))

    def test_all_failed_chunks_are_a_tool_error(self):
        with patch.object(bridge,'invoke',side_effect=TimeoutError('deadline')):
            response=bridge.workflow('review_diff',self.args())
        self.assertTrue(response['isError']); self.assertEqual(response['structuredContent']['chunks']['completed'],0)

    def test_small_source_and_named_test_share_one_request(self):
        (self.root/'test_calc.py').write_text('from calc import mean\nassert mean([2,4]) == 3\n',encoding='utf-8')
        with patch.object(bridge,'invoke',return_value=(report([finding()]),{})) as invoked:
            result=bridge.workflow('review_diff',self.args(files=['calc.py','test_calc.py']))['structuredContent']
        self.assertEqual(invoked.call_count,1); self.assertEqual(len(result['chunks']['results'][0]['focuses']),2)

    def test_cancellation_preserves_finished_evidence_and_marks_freshness_unknown(self):
        cancelled=threading.Event()
        def first(*args,**kwargs):
            cancelled.set(); return report([finding()]),{}
        with patch.object(bridge,'invoke',side_effect=first) as invoked:
            response=bridge.workflow('review_diff',self.args(),cancelled)
        value=response['structuredContent']; self.assertEqual(invoked.call_count,1)
        self.assertEqual(value['chunks']['completed'],1); self.assertTrue(value['findings'])
        self.assertFalse(value['freshness_verified']); self.assertIn('calc.py',value['freshness']['verification_cancelled'])

    def test_test_first_packing_preserves_all_large_source_windows(self):
        (self.root/'test_calc.py').write_text('from calc import mean\n',encoding='utf-8')
        (self.root/'calc.py').write_text('\n'.join(f'value_{i} = {i}' for i in range(700)),encoding='utf-8')
        with context.call_budget(10,wide_context=True):
            bundle=context.collect_review(self.args(files=['test_calc.py','calc.py']))
        plan=chunks.plan(bundle)
        seen=[]
        for chunk in plan['chunks']:
            keys=[(s['path'],s['side']) for s in chunk['snapshots']]
            self.assertEqual(len(keys),len(set(keys)))
            seen.extend(n for s in chunk['snapshots'] if s['path']=='calc.py' for n,_ in chunks.numbers(s))
        self.assertEqual(sorted(seen),list(range(1,701)))


if __name__=='__main__': unittest.main()
