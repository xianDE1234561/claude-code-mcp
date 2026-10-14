"""Bounded module/file review slices and aggregation with explicit incomplete work."""
from copy import deepcopy
import json
import re
from pathlib import PurePosixPath
import review_workflow as review


def numbers(snapshot):
    return [(int(m[1]),m[2]) for line in snapshot['text'].splitlines()
            if (m:=re.match(r'^(\d+): (.*)$',line))]


def slice_snapshot(snapshot,selected):
    ranges=[]
    for number,_ in selected:
        if ranges and number==ranges[-1][1]+1: ranges[-1][1]=number
        else: ranges.append([number,number])
    return {**snapshot,'text':'\n'.join(f'{n}: {text}' for n,text in selected),
            'line_ranges':ranges,'truncated':False}


def windows(snapshot,cap):
    # Prefer Python/module declaration boundaries, then bounded line windows.
    rows=numbers(snapshot); groups=[]; current=[]; size=0
    for row in rows:
        number,text=row; cost=len(text)+20
        declaration=bool(re.match(r'^(?:async\s+def|def|class|export\s+(?:function|class))\s',text))
        if current and (size+cost>cap or (declaration and size>cap//2)):
            groups.append(current); current=[]; size=0
        current.append(row); size+=cost
    if current: groups.append(current)
    return groups


def plan(bundle,chunk_chars=18000,max_chunks=12):
    if type(chunk_chars) is not int or not 6000<=chunk_chars<=30000: raise ValueError('chunk_chars must be 6000–30000.')
    if type(max_chunks) is not int or not 1<=max_chunks<=12: raise ValueError('max_chunks must be 1–12.')
    chunks=[]; unplanned=[]
    primary=bundle['reviewed_paths']; related=[s for s in bundle['snapshots'] if s.get('role')=='related']
    for path in primary:
        snapshots=[s for s in bundle['snapshots'] if s['path']==path and s.get('role')!='related']
        core=next((s for s in snapshots if s['side']=='after'),snapshots[0])
        groups=windows(core,max(1500,chunk_chars//3))
        for rows in groups:
            start,end=rows[0][0],rows[-1][0]
            identity={'path':path,'side':core['side'],'line_ranges':slice_snapshot(core,rows)['line_ranges']}
            if len(chunks)>=max_chunks: unplanned.append({**identity,'reason':'max_chunks limit'}); continue
            selected=[slice_snapshot(core,rows)]
            for other in snapshots:
                if other is core: continue
                # Paired opposite-side windows provide bounded baseline context.
                paired=[r for r in numbers(other) if start-10<=r[0]<=end+10]
                if paired: selected.append(slice_snapshot(other,paired))
            item={**bundle,'snapshots':selected,'diffs':[],'reviewed_paths':[path],'related_paths':[],
                  'absent_after':[p for p in bundle.get('absent_after',[]) if p==path],
                  'chunk':{'id':len(chunks)+1,'focus':identity,'focuses':[identity],
                           'limitations':['Review is local to this window; opposite-side line alignment can differ after edits. Cross-window interactions are not exhaustively reviewed.']}}
            # Diff text is supporting context only and is bounded separately.
            for diff in bundle['diffs']:
                if diff['path']==path:
                    item['diffs'].append({**diff,'text':diff['text'][:2500],'truncated':len(diff['text'])>2500})
            for extra in related:
                tentative={**item,'snapshots':item['snapshots']+[extra]}
                if len(json.dumps(tentative,ensure_ascii=False))<=chunk_chars:
                    item['snapshots'].append(extra); item['related_paths'].append(extra['path'])
            if len(json.dumps(item,ensure_ascii=False))>chunk_chars:
                unplanned.append({**identity,'reason':'chunk context budget'}); continue
            chunks.append(item)
    # Keep a small source module and its named test in one request when they fit.
    packed=[]
    for item in chunks:
        name=PurePosixPath(item['reviewed_paths'][0])
        partner=next((p for p in packed if any(name.parent==PurePosixPath(other).parent and
            (name.stem=='test_'+PurePosixPath(other).stem or PurePosixPath(other).stem=='test_'+name.stem)
            for other in p['reviewed_paths'])),None)
        if partner:
            candidate=deepcopy(partner)
            candidate['reviewed_paths']+=item['reviewed_paths']
            candidate['chunk']['focuses']+=item['chunk']['focuses']
            candidate['diffs']+=item['diffs']; candidate['absent_after']+=item['absent_after']
            primary_names=set(candidate['reviewed_paths'])
            unique={}
            for snap in candidate['snapshots']+item['snapshots']:
                if snap.get('role')=='related' and snap['path'] in primary_names: continue
                key=(snap['path'],snap['side'])
                if key in unique:
                    previous=unique[key]
                    if previous['sha256']!=snap['sha256']: raise ValueError('Conflicting snapshot hashes in a chunk.')
                    rows=dict(numbers(previous)); rows.update(dict(numbers(snap)))
                    unique[key]=slice_snapshot(previous,sorted(rows.items()))
                else: unique[key]=snap
            candidate['snapshots']=list(unique.values())
            candidate['related_paths']=sorted({s['path'] for s in candidate['snapshots'] if s.get('role')=='related'})
            if len(json.dumps(candidate,ensure_ascii=False))<=chunk_chars:
                packed[packed.index(partner)]=candidate; continue
        packed.append(item)
    for index,item in enumerate(packed): item['chunk']['id']=index+1
    return {'chunks':packed,'unplanned':unplanned,'planned_count':len(packed),
            'chunk_chars':chunk_chars,'max_chunks':max_chunks}


def aggregate(mode,bundle,plan,completed,failures):
    # Revalidate raw candidates against exactly the chunk that supplied evidence.
    checked=[]; raw_findings=[]; suggestions=[]; hypotheses=[]; tests=[]; rejected=[]
    for chunk,raw,metadata in completed:
        value=review.validate_findings(raw,chunk,mode)
        for item in value['findings']:
            raw_findings.append({k:item[k] for k in review.FINDING_SCHEMA['properties']})
        suggestions.extend(value['suggestions']); hypotheses.extend(value['hypotheses'])
        tests.extend(value['tests_to_run']); rejected.extend(value['rejected_findings'])
        checked.append({'id':chunk['chunk']['id'],'focus':chunk['chunk']['focus'],'focuses':chunk['chunk']['focuses'],
                        'verdict':value['verdict'],'request':metadata,'rejected_count':len(value['rejected_findings'])})
    merged={'summary':'Aggregated bounded chunk review.','verdict':'issues_found' if raw_findings else 'no_issues_found',
            'findings':raw_findings[:30],'suggestions':suggestions[:20],'hypotheses':hypotheses[:10],
            'tests_to_run':list(dict.fromkeys(tests))[:20]}
    usage={key:sum(m.get('usage',{}).get(key,0) for _,_,m in completed if m.get('model_called',True))
           for key in ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')}
    output=review.finish(mode,merged,bundle,{'model_called':any(m.get('model_called',True) for _,_,m in completed),
        'cache_hit':bool(completed) and all(m.get('cache_hit') for _,_,m in completed),
        'estimated_cost_usd':sum(m.get('estimated_cost_usd',0) for _,_,m in completed),'usage':usage,
        'cost_note':'Sum of CLI estimates for this request; cache hits cost zero.'})
    output['rejected_findings'].extend(rejected)
    pending=list(plan['unplanned'])+failures
    truncated=len(raw_findings)>30 or len(suggestions)>20 or len(hypotheses)>10
    insufficient=any(item['verdict']=='insufficient_context' for item in checked)
    complete=len(completed)==plan['planned_count'] and not pending and not truncated
    output['chunks']={'planned':plan['planned_count'],'completed':len(completed),'complete':complete,
        'results':checked,'unreviewed':pending,'results_truncated':truncated,
        'limitations':['Bounded local chunks cannot prove whole-project correctness or exhaustively cover cross-chunk dependencies.']}
    if not complete or insufficient or rejected:
        output['coverage']['complete']=False
        output['coverage']['warnings'].append('Chunk review incomplete, insufficient, truncated, or has rejected evidence; inspect chunks.unreviewed and results.')
        if not output['findings']: output['verdict']='insufficient_context'
    output['verification_status']='partial' if not complete else 'completed'
    return output
