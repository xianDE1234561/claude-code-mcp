"""Dependency-free STDIO MCP bridge to local Claude Code (no shell, no tools)."""
import concurrent.futures
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import review_workflow
from windows_job import Job
from workflow_progress import Progress
from volatile_cache import REVIEW_CACHE

ROOT = Path(__file__).resolve().parent
MAX_PROMPT = 100000
MAX_OUTPUT = 100000
OUTPUT_LOCK = threading.Lock()
CALL_LOCK = threading.Lock()
CANCEL = {}


def settings_env():
    try:
        data = json.loads((Path.home()/'.claude/settings.json').read_text(encoding='utf-8-sig'))
        return data.get('env', {})
    except (OSError, ValueError):
        return {}


def redact(value):
    return prepare_redactor()(value)


def prepare_redactor():
    secrets=[]
    for key, secret in {**settings_env(), **os.environ}.items():
        if any(word in key.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')):
            if isinstance(secret, str) and len(secret) >= 8:
                secrets.append(secret)
    def clean(value):
        text=str(value)
        for secret in secrets: text=text.replace(secret,'<redacted>')
        return text
    clean.cache_key=hashlib.sha256(json.dumps(sorted(secrets)).encode()).hexdigest()
    return clean


def executable():
    explicit = os.environ.get('CLAUDE_MCP_EXE')
    candidate = Path(explicit) if explicit else Path(os.environ.get('APPDATA', ''))/'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
    if candidate.is_absolute() and candidate.is_file() and candidate.suffix.lower() == '.exe':
        return str(candidate)
    native = shutil.which('claude.exe')
    if native:
        return native
    raise ValueError('Native claude.exe not found; set CLAUDE_MCP_EXE to its absolute path.')


def run_cli(args, prompt=None, timeout=15, cancelled=None):
    env = dict(os.environ)
    env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    # File-backed capture avoids Windows reader threads waiting on inherited
    # pipe handles after the primary process exits. Files are temporary only.
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        job=Job(); process=None
        try:
            stdin.write(prompt.encode('utf-8') if prompt is not None else b''); stdin.seek(0)
            process=subprocess.Popen([executable(), *args],stdin=stdin,
                stdout=stdout,stderr=stderr,cwd=ROOT,env=env,creationflags=flags,shell=False)
            job.assign(process)
            deadline=time.monotonic()+timeout
            if cancelled is not None and cancelled.is_set(): raise RuntimeError('Claude call cancelled.')
            while process.poll() is None:
                if cancelled is not None and cancelled.is_set(): raise RuntimeError('Claude call cancelled.')
                remaining=deadline-time.monotonic()
                if remaining<=0: raise TimeoutError(f'Claude did not finish within {timeout} seconds.')
                try: process.wait(timeout=min(.5,remaining))
                except subprocess.TimeoutExpired: pass
            job.close()
            stdout.seek(0); stderr.seek(0)
            out=stdout.read(2_000_001); err=stderr.read(200001)
            if len(out)>2_000_000 or len(err)>200000: raise ValueError('CLI diagnostic/output exceeds capture limit.')
            return process.returncode,out.decode('utf-8',errors='replace'),err.decode('utf-8',errors='replace')
        finally:
            job.close()
            if process is not None:
                if process.poll() is None: process.kill()
                process.wait(timeout=5)


def result(text, failed=False):
    text = redact(text)
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT]+'\n[Output truncated by bridge]'
    return {'content':[{'type':'text','text':text}], 'isError':failed}


def result_json(value, failed=False):
    # Redact values before encoding so quotes/backslashes cannot corrupt JSON.
    sanitizer=prepare_redactor()
    def clean(item):
        if isinstance(item,str): return sanitizer(item)
        if isinstance(item,list): return [clean(v) for v in item]
        if isinstance(item,dict): return {k:clean(v) for k,v in item.items()}
        return item
    value=clean(value)
    text=json.dumps(value,ensure_ascii=False)
    if len(text)>MAX_OUTPUT:
        return result_json({'error':'Structured result exceeds output limit; narrow the review scope.'},True)
    return {'content':[{'type':'text','text':text}],'structuredContent':value,'isError':failed}


def model_options(arguments):
    model=arguments.get('model',os.environ.get('CLAUDE_MCP_MODEL',''))
    timeout=arguments.get('timeout_seconds',180)
    if not isinstance(model,str) or len(model)>160 or model.startswith('-') or any(c.isspace() for c in model):
        raise ValueError('Invalid model identifier.')
    if type(timeout) is not int or not 10 <= timeout <= 600:
        raise ValueError('timeout_seconds must be an integer from 10 to 600.')
    return model,timeout


def invoke(text,model,timeout,cancelled,system,schema=None):
    if len(text)>MAX_PROMPT:
        raise ValueError(f'Combined prompt/context limit: {MAX_PROMPT} characters.')
    args=['-p','--output-format','json','--safe-mode','--disable-slash-commands','--tools','',
          '--strict-mcp-config','--no-session-persistence','--permission-mode','dontAsk',
          '--system-prompt',system]
    if model: args.extend(['--model',model])
    if schema: args.extend(['--json-schema',json.dumps(schema,separators=(',',':'))])
    start=time.monotonic()
    code,out,err=run_cli(args,text,timeout,cancelled)
    try: payload=json.loads(out)
    except ValueError:
        raise ValueError(f'Invalid CLI JSON (exit={code}): {out[-2000:]}\n{err[-2000:]}')
    if not isinstance(payload,dict): raise ValueError('CLI returned an unexpected JSON type.')
    if code or payload.get('is_error'):
        raise RuntimeError(f'Claude request failed (exit={code}): {payload.get("result") or payload.get("errors") or err[-2000:]}')
    metadata={'elapsed_seconds':round(time.monotonic()-start,2),'models':list(payload.get('modelUsage',{})),
              'permission_denials':payload.get('permission_denials',[])}
    if isinstance(payload.get('usage'),dict): metadata['usage']=payload['usage']
    if isinstance(payload.get('total_cost_usd'),(int,float)):
        metadata['estimated_cost_usd']=payload['total_cost_usd']
        metadata['cost_note']='CLI estimate; may differ from provider billing.'
    if schema:
        structured=payload.get('structured_output')
        if structured is None:
            # Some compatible providers emit raw JSON instead of structured_output.
            try: structured=json.loads(payload.get('result',''))
            except (TypeError,ValueError): raise ValueError('Provider returned no structured JSON report.')
        return structured,metadata
    answer=payload.get('result')
    if not isinstance(answer,str) or not answer.strip(): raise ValueError('Claude returned no final answer.')
    return answer,metadata


def ask(arguments, cancelled=None):
    if not isinstance(arguments, dict) or set(arguments)-{'prompt','context','model','timeout_seconds'}:
        return result('Unknown arguments or invalid argument object.', True)
    prompt=arguments.get('prompt')
    context=arguments.get('context','')
    if not isinstance(prompt,str) or not prompt.strip() or not isinstance(context,str):
        return result('prompt must be non-empty text and context must be text.',True)
    if len(prompt)+len(context) > MAX_PROMPT:
        return result(f'Combined prompt/context limit: {MAX_PROMPT} characters.',True)
    text=prompt if not context else f'Context supplied by caller:\n{context}\n\nTask:\n{prompt}'
    try:
        model,timeout=model_options(arguments)
        answer,metadata=invoke(text,model,timeout,cancelled,
            'You are an independent advisor. Answer using only the prompt and explicitly supplied context. You have no tools or access to workspace files. Do not claim to have executed commands or changed files.')
        return result_json({'answer':answer,**metadata})
    except (OSError,ValueError,RuntimeError,TimeoutError) as exc:
        return result(str(exc),True)


def workflow(mode,arguments,cancelled=None,on_progress=None):
    progress=Progress(on_progress)
    try:
        if not isinstance(arguments,dict): raise ValueError('arguments must be an object.')
        model,timeout=model_options(arguments)
        with review_workflow.context.call_budget(timeout,cancelled,use_cache=arguments.get('use_cache',True)):
            progress.set('collect_context'); sanitizer=prepare_redactor()
            bundle=review_workflow.collect(mode,arguments,sanitizer)
            if mode=='review_diff' and not bundle.get('has_changes',True):
                progress.set('no_changes'); output=review_workflow.no_changes(mode,bundle)
                output['timing']=progress.summary(); return result_json(output)
            text=review_workflow.prompt(mode,bundle)
            settings_stamps=[]
            for path in (Path.home()/'.claude/settings.json',Path.home()/'.claude.json'):
                try: settings_stamps.append(path.stat().st_mtime_ns)
                except OSError: settings_stamps.append(None)
            key=hashlib.sha256(json.dumps([mode,text,model,review_workflow.SYSTEM,review_workflow.SCHEMA,
                sanitizer.cache_key,settings_stamps,os.environ.get('ANTHROPIC_MODEL'),
                os.environ.get('ANTHROPIC_BASE_URL'),os.environ.get('CLAUDE_MCP_EXE')],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            hit=REVIEW_CACHE.get(key) if arguments.get('use_cache',True) else None
            if hit:
                progress.set('cache_hit'); (report,original),age=hit
                metadata={'model_called':False,'cache_hit':True,'cache_age_seconds':round(age,3),
                          'elapsed_seconds':0,'estimated_cost_usd':0,'original_request':original}
            else:
                progress.set('request_model')
                remaining=review_workflow.context.budget_remaining(timeout)
                report,metadata=invoke(text,model,remaining,cancelled,review_workflow.SYSTEM,review_workflow.SCHEMA)
                metadata.update({'model_called':True,'cache_hit':False})
            progress.set('validate_result')
            output=review_workflow.finish(mode,report,bundle,metadata)
            if not hit and arguments.get('use_cache',True) and output['coverage']['complete'] and output['freshness_verified'] and not output['rejected_findings']:
                REVIEW_CACHE.put(key,(report,metadata))
            progress.set('completed'); output['timing']=progress.summary()
            return result_json(output)
    except (OSError,ValueError,RuntimeError,TimeoutError,subprocess.TimeoutExpired) as exc:
        phase=progress.stage; progress.set('failed')
        return result_json({'workflow':mode,'error':str(exc),'failed_phase':phase,'timing':progress.summary()},True)
    finally: progress.close()


def status(arguments, cancelled=None):
    if not isinstance(arguments,dict) or set(arguments)-{'live_probe'} or type(arguments.get('live_probe',False)) is not bool:
        return result('live_probe must be a boolean; no other arguments accepted.',True)
    try:
        version_code,version,_=run_cli(['--version'])
        auth_code,auth,auth_err=run_cli(['auth','status','--json'])
        auth_json=json.loads(auth) if auth.strip() else {}
        url=urlsplit(os.environ.get('ANTHROPIC_BASE_URL',settings_env().get('ANTHROPIC_BASE_URL','https://api.anthropic.com')))
        info={'version':version.strip(),'executable':executable(),'cli_available':version_code==0,
              'auth_status_exit_code':auth_code,
              'auth':{k:auth_json.get(k) for k in ('loggedIn','authMethod','apiProvider')},
              'endpoint':f'{url.scheme}://{url.hostname or "unknown"}'+(f':{url.port}' if url.port else ''),
              'default_model':os.environ.get('CLAUDE_MCP_MODEL') or 'Claude CLI configured default',
              'mode':'Independent prompt/context only; built-in tools, customizations and nested MCP disabled.',
              'connectivity_verified':False}
        info['workflow_tools']=['review_diff','diagnose_failure']
        info['allowed_roots']=[str(p) for p in review_workflow.context.allowed_roots()]
        if arguments.get('live_probe'):
            probe=ask({'prompt':'Reply with exactly CLAUDE_MCP_OK','timeout_seconds':90},cancelled)
            info['live_probe']=json.loads(probe['content'][0]['text']) if not probe['isError'] else probe['content'][0]['text']
            info['connectivity_verified']=not probe['isError'] and info['live_probe'].get('answer','').strip()=='CLAUDE_MCP_OK'
        failed=not info['cli_available'] or (arguments.get('live_probe') and not info['connectivity_verified'])
        return result(json.dumps(info,ensure_ascii=False),bool(failed))
    except (OSError,ValueError,RuntimeError,TimeoutError) as exc:
        return result(str(exc),True)


TOOLS=[
    {'name':'claude_status','description':'Inspect local Claude Code version and authentication without revealing credentials. Set live_probe=true to verify an actual model response (consumes provider usage).',
     'inputSchema':{'type':'object','properties':{'live_probe':{'type':'boolean','default':False}},'additionalProperties':False},
     'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':True}},
    {'name':'ask_claude','description':'Ask local Claude Code for an independent answer, code review, or analysis. Supply all relevant context explicitly. No built-in tools, filesystem access, shell, nested MCP, or shared conversation. Uses the existing Claude provider and credentials; consumes provider usage.',
     'inputSchema':{'type':'object','properties':{'prompt':{'type':'string','minLength':1,'maxLength':MAX_PROMPT},'context':{'type':'string','maxLength':MAX_PROMPT},'model':{'type':'string','maxLength':160},'timeout_seconds':{'type':'integer','minimum':10,'maximum':600,'default':180}},'required':['prompt'],'additionalProperties':False},
     'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':True}},
]

COMMON_WORKFLOW_PROPERTIES={
    'project_root':{'type':'string','description':'Absolute directory under configured allowed roots.'},
    'files':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':24,
             'description':'Explicit source file paths relative to project_root; required for files scope and diagnosis.'},
    'requirements':{'type':'string','maxLength':10000,'description':'Task requirements and acceptance conditions.'},
    'model':{'type':'string','maxLength':160},
    'timeout_seconds':{'type':'integer','minimum':10,'maximum':600,'default':180},
    'include_related':{'type':'boolean','default':True,'description':'Read at most four relevant local imports/tests under the same project root.'},
    'use_cache':{'type':'boolean','default':True,'description':'Reuse a complete matching review for up to five minutes within this server process; false forces a fresh model request.'},
}
TOOLS.extend([
    {'name':'review_diff','description':'Independently review Git changes or explicit files using bounded automatic source snapshots, line numbers and SHA-256 hashes. Returns structured findings with locally checked evidence, partial coverage and stale-source warnings. No code execution or edits. Consumes Claude provider usage.',
     'inputSchema':{'type':'object','properties':{**COMMON_WORKFLOW_PROPERTIES,
        'scope':{'type':'string','enum':['working_tree','staged','unstaged','files'],'default':'working_tree'},
        'base_ref':{'type':'string','default':'HEAD','description':'Commit baseline for working_tree only.'}},
        'required':['project_root'],'additionalProperties':False},
     'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':True}},
    {'name':'diagnose_failure','description':'Analyze a supplied failure log and explicit source files. Returns source-backed findings and separate log-backed hypotheses with verification suggestions. Collects files automatically; never executes the failed command or tests. Consumes Claude provider usage.',
     'inputSchema':{'type':'object','properties':{**COMMON_WORKFLOW_PROPERTIES,
        'log':{'type':'string','maxLength':50000},'log_file':{'type':'string','description':'Project-relative text log path.'},
        'command':{'type':'string','maxLength':10000,'description':'Failed command as context only; not executed.'}},
        'required':['project_root','files'],'additionalProperties':False},
     'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':True}},
])


def send(message):
    with OUTPUT_LOCK:
        sys.stdout.buffer.write((json.dumps(message,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8'))
        sys.stdout.buffer.flush()


def reply(rid,payload):
    send({'jsonrpc':'2.0','id':rid,'result':payload})


def error(rid,code,text):
    send({'jsonrpc':'2.0','id':rid,'error':{'code':code,'message':redact(text)}})


def call(rid,name,arguments,event,progress_token=None):
    try:
        def progress(update):
            send({'jsonrpc':'2.0','method':'notifications/progress','params':{'progressToken':progress_token,**update}})
        if name in ('review_diff','diagnose_failure'):
            output=workflow(name,arguments,event,progress if progress_token is not None else None)
        else: output=(status if name=='claude_status' else ask)(arguments,event)
        reply(rid,output)
    except Exception as exc:
        error(rid,-32603,str(exc))
    finally:
        CANCEL.pop(rid,None)
        CALL_LOCK.release()


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        try:
            for line in sys.stdin.buffer:
                try:
                    message=json.loads(line)
                except ValueError:
                    error(None,-32700,'Invalid JSON'); continue
                if not isinstance(message,dict) or not isinstance(message.get('method'),str):
                    error(message.get('id') if isinstance(message,dict) else None,-32600,'Invalid request'); continue
                method=message['method']; params=message.get('params',{})
                if method=='notifications/cancelled' and isinstance(params,dict):
                    cancelled_id=params.get('requestId')
                    event=CANCEL.get(cancelled_id) if isinstance(cancelled_id,(str,int)) else None
                    if event: event.set()
                if 'id' not in message: continue
                rid=message['id']
                if not isinstance(params,dict) or not isinstance(rid,(str,int)) or isinstance(rid,bool):
                    error(rid,-32602,'Invalid request parameters or id'); continue
                if method=='initialize':
                    offered=params.get('protocolVersion')
                    version=offered if offered in ('2024-11-05','2025-03-26','2025-06-18','2025-11-25') else '2025-06-18'
                    reply(rid,{'protocolVersion':version,'capabilities':{'tools':{'listChanged':False}},
                        'serverInfo':{'name':'claude-code-bridge','version':'3.0.0'},
                        'instructions':'Use review_diff for code review and diagnose_failure for logs. findings are candidates, not confirmed defects; suggestions are optional. Check freshness, coverage, behavior evidence and verification_steps before reproducing and fixing. Provide requirements and narrow files. Progress/timing identify the current stage; no_changes never calls a model. use_cache=false requests a fresh review. Claude cannot run tests or edit files. Default to two review rounds; only one call runs at a time.'})
                elif method=='ping': reply(rid,{})
                elif method=='tools/list': reply(rid,{'tools':TOOLS})
                elif method=='tools/call':
                    name=params.get('name'); arguments=params.get('arguments',{})
                    if name not in ('ask_claude','claude_status','review_diff','diagnose_failure') or not isinstance(arguments,dict):
                        error(rid,-32602,'Unknown tool or invalid arguments')
                    elif not CALL_LOCK.acquire(blocking=False):
                        reply(rid,result('A Claude call is already running; retry after it finishes.',True))
                    else:
                        event=threading.Event(); CANCEL[rid]=event
                        meta=params.get('_meta',{})
                        token=meta.get('progressToken') if isinstance(meta,dict) else None
                        if not isinstance(token,(str,int)) or isinstance(token,bool): token=None
                        executor.submit(call,rid,name,arguments,event,token)
                elif method=='resources/list': reply(rid,{'resources':[]})
                elif method=='resources/templates/list': reply(rid,{'resourceTemplates':[]})
                elif method=='prompts/list': reply(rid,{'prompts':[]})
                else: error(rid,-32601,'Unknown method')
        finally:
            for event in list(CANCEL.values()): event.set()


if __name__=='__main__':
    main()
