"""Finite-contract benchmark. Offline oracles are separate from live model quality."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from mcp_client import Client

ROOT=Path(__file__).resolve().parent


def load_cases():
    cases=json.loads((ROOT/'quality_cases/cases.json').read_text(encoding='utf-8'))
    for case in cases:
        name=Path(case['file'])
        if len(name.parts)!=1 or name.suffix!='.py' or ':' in case['file']: raise ValueError('Benchmark file must be a plain Python filename.')
    return cases


def matches(finding,case):
    def contains(field,needles):
        return any(needle.casefold() in finding.get(field,'').casefold() for needle in needles)
    return finding.get('file')==case['file'] and any(finding['line_start']<=line<=finding['line_end'] for line in case['defect_lines']) and contains('trigger',case['trigger_needles']) and contains('actual_behavior',case['actual_needles']) and contains('expected_behavior',case['expected_needles'])


def metrics(records):
    successful=[r for r in records if r.get('model_report_complete')]
    tp=sum(r['defect'] and r['matched_candidates']>0 for r in successful)
    fn=sum(r['defect'] and r['matched_candidates']==0 for r in successful)
    fp=sum(not r['defect'] and r['candidate_count']>0 for r in successful)
    tn=sum(not r['defect'] and r['candidate_count']==0 for r in successful)
    accepted=sum(r['candidate_count'] for r in successful); rejected=sum(r['rejected_count'] for r in successful)
    elapsed=[r['elapsed_seconds'] for r in records]
    return {'completed_cases':len(successful),'failed_or_partial_cases':len(records)-len(successful),
            'true_positive_cases':tp,'false_negative_cases':fn,'false_positive_cases':fp,'true_negative_cases':tn,
            'candidate_recall':tp/(tp+fn) if tp+fn else None,
            'negative_case_false_positive_rate':fp/(fp+tn) if fp+tn else None,
            'evidence_validity_rate':accepted/(accepted+rejected) if accepted+rejected else None,
            'unmatched_positive_case_candidates':sum(r['candidate_count']-r['matched_candidates'] for r in successful if r['defect']),
            'median_elapsed_seconds':statistics.median(elapsed) if elapsed else None,
            'measurement_note':'Finite-contract case-level candidate matching; quotations are not semantic proof. Failed/partial cases are reported separately, not silently counted as correct.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Call the existing Claude provider; consumes usage.')
    parser.add_argument('--timeout',type=int,default=150,help='Total budget per case.')
    parser.add_argument('--output',type=Path,help='Local result path; do not publish raw reports.')
    options=parser.parse_args()
    if not 10<=options.timeout<=600: parser.error('--timeout must be 10–600.')
    parent=ROOT/'test-artifacts'; parent.mkdir(exist_ok=True)
    output=options.output or parent/('quality-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    records=[]; reports={}; client=None
    try:
        if options.live: client=Client(on_progress=lambda event: print('[quality] '+event.get('message',''),flush=True))
        with tempfile.TemporaryDirectory(prefix='quality-',dir=parent) as directory:
            work=Path(directory)
            for case in load_cases():
                project=work/case['id']; project.mkdir(); (project/case['file']).write_text(case['source'],encoding='utf-8')
                oracle=subprocess.run([sys.executable,'-c',case['oracle']],cwd=project,capture_output=True,timeout=10)
                if (oracle.returncode!=0)!=case['defect']: raise AssertionError('Oracle disagrees with declared ground truth: '+case['id'])
                record={'id':case['id'],'defect':case['defect'],'oracle_exit_code':oracle.returncode}
                if options.live:
                    started=time.monotonic()
                    response=client.call('review_diff',{'project_root':str(project),'scope':'files','files':[case['file']],
                        'requirements':case['requirements'],'include_related':False,'use_cache':False,'timeout_seconds':options.timeout})
                    reports[case['id']]=response; value=response.get('structuredContent',{})
                    findings=value.get('findings',[])
                    record.update({'elapsed_seconds':round(time.monotonic()-started,3),
                        'model_report_complete':not response.get('isError',True) and value.get('chunks',{}).get('complete',False) and value.get('freshness_verified',False) and value.get('coverage',{}).get('complete',False) and value.get('verdict')!='insufficient_context',
                        'candidate_count':len(findings),'matched_candidates':sum(matches(f,case) for f in findings),
                        'rejected_count':len(value.get('rejected_findings',[]))})
                records.append(record)
                result={'live_model_measurement':options.live,'records':records,'raw_reports':reports,
                        'metrics':metrics(records) if options.live else None,
                        'note':'Offline oracle validation does not measure model quality. Raw live reports remain local.'}
                output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(record,ensure_ascii=False),flush=True)
    finally:
        if client: client.close()
    if options.live: print(json.dumps(metrics(records),ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
