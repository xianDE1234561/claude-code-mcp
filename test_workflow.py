import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import bridge
import context_bundle as context
import review_workflow as review
from volatile_cache import REVIEW_CACHE,SOURCE_CACHE


def report(findings=None,hypotheses=None):
    return {'summary':'审查完成','verdict':'issues_found' if findings else 'no_issues_found',
            'findings':findings or [],'hypotheses':hypotheses or [],'suggestions':[],'tests_to_run':['运行测试']}


def finding(**changes):
    value={'severity':'P2','title':'Division by zero','file':'calc.py','side':'after',
        'line_start':2,'line_end':2,'evidence':'return sum(xs) / len(xs)',
        'explanation':'空列表会失败','suggestion':'明确处理空列表',
        'category':'behavior','issue_key':'empty_list_division','trigger':'mean([])',
        'expected_behavior':'Handle empty input according to the stated contract',
        'actual_behavior':'ZeroDivisionError','basis_kind':'source_contract',
        'basis_quote':'return sum(xs) / len(xs)','verification_steps':['运行 mean([])，检查异常类型']}
    return {**value,**changes}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        REVIEW_CACHE.clear(); SOURCE_CACHE.clear()
        artifact_root=context.ROOT/'test-artifacts'
        artifact_root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='unit-',dir=artifact_root)
        self.root=Path(self.temp.name).resolve()
        assert self.root.is_relative_to(artifact_root.resolve())
        self.env=patch.dict(os.environ,{'CLAUDE_MCP_ALLOWED_ROOTS':json.dumps([str(self.root)])})
        self.env.start()
        self.write('calc.py','def mean(xs):\n    return sum(xs) / len(xs)\n')

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def write(self,name,text):
        path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text,encoding='utf-8'); return path

    def bundle(self):
        return context.collect_review({'project_root':str(self.root),'scope':'files','files':['calc.py']})

    def init_git(self):
        context.git(self.root,['init','-q'])
        context.git(self.root,['add','--','calc.py'])
        context.git(self.root,['-c','user.name=MCP Test','-c','user.email=test@example.invalid','commit','-qm','baseline'])

    def test_files_snapshot_hash_and_lines(self):
        bundle=self.bundle(); source=bundle['snapshots'][0]
        self.assertIn('2:     return sum(xs) / len(xs)',source['text'])
        self.assertEqual(source['sha256'],hashlib.sha256((self.root/'calc.py').read_bytes()).hexdigest())
        self.assertFalse(bundle['warnings'])

    def test_reject_outside_root_traversal_secrets_and_ads(self):
        with self.assertRaises(ValueError): context.project_path(str(self.root.parent))
        for path in ('../calc.py',str(self.root/'calc.py'),'.env','.aws/credentials',
                     '.codex/config.toml','calc.py:secret','node_modules/a.js','private.pem'):
            with self.subTest(path=path),self.assertRaises(ValueError): context.source_path(self.root,path)

    def test_symlink_escape_if_supported(self):
        outside=self.root.parent/'outside-test.txt'
        outside.write_text('private',encoding='utf-8')
        try:
            try: (self.root/'linked.txt').symlink_to(outside)
            except OSError: self.skipTest('Windows symlink creation requires privileges on this host.')
            with self.assertRaises(ValueError): context.source_path(self.root,'linked.txt')
        finally: outside.unlink(missing_ok=True)

    def test_sensitive_directory_as_project_rejected(self):
        hidden=self.root/'.aws'; hidden.mkdir()
        with self.assertRaises(ValueError): context.project_path(str(hidden))

    def test_credentials_scrubbed_before_prompt(self):
        self.write('sample.json','{"api_key":"secret-value-12345"}')
        bundle=context.collect_review({'project_root':str(self.root),'scope':'files','files':['sample.json']})
        self.assertNotIn('secret-value-12345',review.prompt('review_diff',bundle))
        self.assertIn('<redacted>',bundle['snapshots'][0]['text'])

    def test_partial_coverage_is_explicit(self):
        bundle=context.collect_review({'project_root':str(self.root),'scope':'files','files':['calc.py','.env']})
        self.assertTrue(bundle['warnings']); self.assertEqual(len(bundle['skipped']),1)
        value=review.validate_findings(report(),bundle,'review_diff')
        self.assertFalse(value['coverage']['complete']); self.assertEqual(value['verdict'],'insufficient_context')

    def test_oversized_and_binary_files_skipped(self):
        (self.root/'large.py').write_bytes(b'x'*(context.MAX_FILE_BYTES+1))
        (self.root/'binary.py').write_bytes(b'abc\x00def')
        bundle=context.collect_review({'project_root':str(self.root),'scope':'files','files':['large.py','binary.py']})
        self.assertFalse(bundle['snapshots']); self.assertEqual(len(bundle['skipped']),2)

    def test_grounded_findings_accepted_and_fabricated_locations_rejected(self):
        value=review.validate_findings(report([finding(),finding(line_start=100,line_end=100),
            finding(file='invented.py'),finding(evidence='return 42')]),self.bundle(),'review_diff')
        self.assertEqual(len(value['findings']),1)
        self.assertEqual(len(value['rejected_findings']),3)
        self.assertTrue(value['findings'][0]['location_verified'])

    def test_schema_error_fails_closed(self):
        malformed=report(); malformed['findings']=[{'severity':'P2'}]
        with self.assertRaises(ValueError): review.validate_findings(malformed,self.bundle(),'review_diff')

    def test_insufficient_context_verdict_is_preserved(self):
        value=report(); value['verdict']='insufficient_context'
        self.assertEqual(review.validate_findings(value,self.bundle(),'review_diff')['verdict'],'insufficient_context')

    def test_disabling_cache_also_disables_source_cache(self):
        sanitizer=bridge.prepare_redactor()
        with context.call_budget(10,use_cache=False):
            with patch.object(SOURCE_CACHE,'get',side_effect=AssertionError('source cache used')):
                context.snapshot(self.root,'calc.py',sanitizer=sanitizer)
            self.assertFalse(SOURCE_CACHE.values)

    def test_hypotheses_require_exact_log_evidence(self):
        bundle=context.collect_failure({'project_root':str(self.root),'files':['calc.py'],
            'log':'ZeroDivisionError: division by zero'})
        hypotheses=[{'cause':'empty list','log_evidence':'ZeroDivisionError: division by zero','verification':'调用 mean([])'},
                    {'cause':'invented','log_evidence':'HTTP 401','verification':'无'}]
        value=review.validate_findings(report(hypotheses=hypotheses),bundle,'diagnose_failure')
        self.assertEqual(len(value['hypotheses']),1); self.assertEqual(len(value['rejected_findings']),1)

    def test_stale_file_detected(self):
        bundle=self.bundle(); self.write('calc.py','def mean(xs):\n    return 0\n')
        result=review.finish('review_diff',report([finding()]),bundle,{})
        self.assertEqual(result['stale_files'],['calc.py']); self.assertFalse(result['coverage']['complete'])

    def test_git_working_tree_includes_staged_unstaged_and_untracked(self):
        self.init_git()
        self.write('calc.py','def mean(xs):\n    return 1\n'); context.git(self.root,['add','--','calc.py'])
        self.write('calc.py','def mean(xs):\n    return 2\n'); self.write('new.py','x = 3\n')
        bundle=context.collect_review({'project_root':str(self.root)})
        after={s['path']:s['text'] for s in bundle['snapshots'] if s['side']=='after'}
        self.assertIn('return 2',after['calc.py']); self.assertIn('new.py',after)
        self.assertTrue(bundle['base_commit'])

    def test_git_staged_uses_index_not_working_file(self):
        self.init_git(); self.write('calc.py','def mean(xs):\n    return 1\n')
        context.git(self.root,['add','--','calc.py']); self.write('calc.py','def mean(xs):\n    return 2\n')
        bundle=context.collect_review({'project_root':str(self.root),'scope':'staged'})
        after=next(s for s in bundle['snapshots'] if s['side']=='after')
        self.assertIn('return 1',after['text']); self.assertNotIn('return 2',after['text'])
        self.assertFalse(context.stale_snapshots(bundle))

    def test_git_unstaged_before_is_index(self):
        self.init_git(); self.write('calc.py','def mean(xs):\n    return 1\n'); context.git(self.root,['add','--','calc.py'])
        self.write('calc.py','def mean(xs):\n    return 2\n')
        bundle=context.collect_review({'project_root':str(self.root),'scope':'unstaged'})
        before=next(s for s in bundle['snapshots'] if s['side']=='before')
        self.assertIn('return 1',before['text'])

    def test_git_staged_deletion_preserves_before_snapshot(self):
        self.init_git(); context.git(self.root,['rm','--','calc.py'])
        bundle=context.collect_review({'project_root':str(self.root),'scope':'staged'})
        self.assertEqual([s['side'] for s in bundle['snapshots']],['before'])

    def test_deleted_file_recreated_during_review_is_stale(self):
        self.init_git(); (self.root/'calc.py').unlink()
        bundle=context.collect_review({'project_root':str(self.root)})
        self.write('calc.py','x = 2\n')
        self.assertEqual(context.stale_snapshots(bundle),['calc.py'])

    def test_git_nested_project_and_explicit_filter(self):
        self.init_git(); self.write('sub/a.py','x=1\n'); context.git(self.root,['add','--','sub/a.py'])
        context.git(self.root,['-c','user.name=MCP Test','-c','user.email=test@example.invalid','commit','-qm','sub'])
        self.write('sub/a.py','x=2\n'); self.write('calc.py','x=4\n')
        bundle=context.collect_review({'project_root':str(self.root/'sub'),'files':['a.py']})
        self.assertEqual({s['path'] for s in bundle['snapshots']},{'sub/a.py'})
        self.assertFalse(context.stale_snapshots(bundle))

    def test_provider_schema_and_workflow_return_structured_content(self):
        payload={'structured_output':report([finding()]),'usage':{'input_tokens':12},'total_cost_usd':0.01}
        with patch.object(bridge,'run_cli',return_value=(0,json.dumps(payload),'')) as run:
            value=bridge.workflow('review_diff',{'project_root':str(self.root),'scope':'files','files':['calc.py']})
        self.assertFalse(value['isError']); self.assertIn('findings',value['structuredContent'])
        self.assertIn('--json-schema',run.call_args.args[0])
        self.assertEqual(value['structuredContent']['request']['usage']['input_tokens'],12)

    def test_failed_command_is_only_data(self):
        args={'project_root':str(self.root),'files':['calc.py'],'log':'ZeroDivisionError',
              'command':'Remove-Item -Recurse C:\\important'}
        payload={'structured_output':report()}
        with patch.object(bridge,'run_cli',return_value=(0,json.dumps(payload),'')) as run:
            result=bridge.workflow('diagnose_failure',args)
        self.assertFalse(result['isError'])
        self.assertNotIn(args['command'],run.call_args.args[0])
        self.assertIn('Remove-Item',run.call_args.args[1])

    def test_context_budget_and_cancellation(self):
        event=threading.Event(); event.set()
        with context.call_budget(20,event),self.assertRaisesRegex(RuntimeError,'cancelled'):
            self.bundle()
        with context.call_budget(-1),self.assertRaises(TimeoutError): self.bundle()

    def test_advisory_is_separate_from_defects(self):
        value=review.validate_findings(report([finding(category='documentation',title='缺少契约说明')]),self.bundle(),'review_diff')
        self.assertFalse(value['findings']); self.assertEqual(len(value['suggestions']),1)
        self.assertEqual(value['verdict'],'no_issues_found')

    def test_behavior_requires_matching_basis_and_verification_steps(self):
        value=review.validate_findings(report([finding(basis_kind='requirement',basis_quote='Imagined requirement'),
            finding(verification_steps=[])]),self.bundle(),'review_diff')
        self.assertFalse(value['findings']); self.assertEqual(len(value['rejected_findings']),2)

    def test_duplicate_defect_locations_are_merged(self):
        value=review.validate_findings(report([finding(),finding(title='Same underlying failure')]),self.bundle(),'review_diff')
        self.assertEqual(len(value['findings']),1); self.assertEqual(value['deduplicated_count'],1)
        self.assertEqual(value['findings'][0]['verification_status'],'candidate')
        self.assertEqual(len(value['findings'][0]['related_locations']),1)

    def test_shared_issue_key_does_not_merge_different_behavior(self):
        value=review.validate_findings(report([finding(),finding(trigger='mean(None)',actual_behavior='TypeError')]),self.bundle(),'review_diff')
        self.assertEqual(len(value['findings']),2)
        self.assertNotEqual(value['findings'][0]['finding_id'],value['findings'][1]['finding_id'])

    def test_behavioral_contract_violation_is_not_filtered_as_documentation(self):
        value=review.validate_findings(report([finding(title='缺少空列表前置校验，违反异常契约')]),self.bundle(),'review_diff')
        self.assertEqual(len(value['findings']),1); self.assertFalse(value['suggestions'])

    def test_unreadable_and_timeout_are_not_changed_files(self):
        bundle=self.bundle()
        with patch.object(context,'read_bytes',side_effect=PermissionError('denied')):
            result=context.verify_snapshots(bundle)
        self.assertFalse(result['changed']); self.assertEqual(result['unreadable'][0]['path'],'calc.py')
        with patch.object(context,'read_bytes',side_effect=TimeoutError('expired')):
            result=context.verify_snapshots(bundle)
        self.assertFalse(result['changed']); self.assertEqual(result['verification_timeout'],['calc.py'])

    def test_expired_verification_does_not_claim_source_changed(self):
        bundle=self.bundle()
        with patch.object(context,'read_bytes',side_effect=TimeoutError('expired')):
            value=review.finish('review_diff',report([finding()]),bundle,{})
        self.assertFalse(value['stale_files']); self.assertFalse(value['freshness_verified'])
        self.assertFalse(value['coverage']['complete'])

    def test_empty_git_change_scope_never_calls_provider(self):
        self.init_git()
        with patch.object(bridge,'invoke') as invoke:
            value=bridge.workflow('review_diff',{'project_root':str(self.root)})
        self.assertFalse(value['isError']); self.assertEqual(value['structuredContent']['verdict'],'no_changes')
        invoke.assert_not_called()

    def test_new_git_repository_without_commit_collects_files(self):
        context.git(self.root,['init','-q']); context.git(self.root,['add','--','calc.py'])
        bundle=context.collect_review({'project_root':str(self.root)})
        self.assertIsNone(bundle['base_commit'])
        self.assertEqual(bundle['reviewed_paths'],['calc.py'])
        self.assertTrue(bundle['snapshots'])

    def test_local_import_and_test_context_is_bounded(self):
        self.write('calc.py','from helper import add\n\ndef calc(x):\n    return add(x)\n')
        self.write('helper.py','def add(x):\n    return x+1\n')
        self.write('test_calc.py','from calc import calc\nassert calc(1)==2\n')
        bundle=self.bundle()
        self.assertEqual(set(bundle['related_paths']),{'helper.py','test_calc.py'})
        disabled=context.collect_review({'project_root':str(self.root),'scope':'files','files':['calc.py'],'include_related':False})
        self.assertFalse(disabled['related_paths'])

    def test_review_cache_reuses_only_matching_context(self):
        args={'project_root':str(self.root),'scope':'files','files':['calc.py']}
        payload={'structured_output':report()}
        with patch.object(bridge,'run_cli',return_value=(0,json.dumps(payload),'')) as run:
            first=bridge.workflow('review_diff',args); second=bridge.workflow('review_diff',args)
            self.assertEqual(run.call_count,1)
            self.assertTrue(second['structuredContent']['request']['cache_hit'])
            self.assertFalse(second['structuredContent']['request']['model_called'])
            bridge.workflow('review_diff',{**args,'use_cache':False}); self.assertEqual(run.call_count,2)
            self.write('calc.py','x=2\n'); bridge.workflow('review_diff',args); self.assertEqual(run.call_count,3)

    def test_stage_timing_and_progress_are_returned(self):
        updates=[]
        with patch.object(bridge,'run_cli',return_value=(0,json.dumps({'structured_output':report()}),'')):
            value=bridge.workflow('review_diff',{'project_root':str(self.root),'scope':'files','files':['calc.py']},on_progress=updates.append)
        stages=value['structuredContent']['timing']['stages_seconds']
        self.assertTrue({'collect_context','request_model','validate_result'}<=set(stages))
        self.assertEqual([u['progress'] for u in updates],list(range(1,len(updates)+1)))
        self.assertTrue(any('收集上下文' in u['message'] for u in updates))


if __name__=='__main__': unittest.main()
