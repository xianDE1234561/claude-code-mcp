"""Call the registered claude_cli STDIO server; useful before UI tool reload."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tomllib


class Client:
    def __init__(self,config_path=None,on_progress=None,startup_timeout=15):
        self.process=None; self.closed=False; self.on_progress=on_progress; self.progress_events=[]
        self.stderr_tail=b''; self.stderr_lock=threading.Lock(); self.reader=None; self.stderr_reader=None
        codex_dir=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
        config=tomllib.loads(Path(config_path or codex_dir/'config.toml').read_text(encoding='utf-8-sig'))
        server=config['mcp_servers']['claude_cli']
        if not server.get('enabled',True): raise ValueError('claude_cli is disabled.')
        env=dict(os.environ); env.update(server.get('env',{}))
        try:
            self.process=subprocess.Popen([server['command'],*server.get('args',[])],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                cwd=server.get('cwd'),env=env,shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            self.messages=queue.Queue(); self.next_id=0
            def read():
                try:
                    for line in self.process.stdout:
                        if len(line)>2_000_000: raise ValueError('MCP message exceeds capture limit.')
                        self.messages.put(json.loads(line))
                    self.messages.put(RuntimeError('MCP server closed stdout.'))
                except Exception as exc: self.messages.put(exc)
            def drain_stderr():
                try:
                    while chunk:=self.process.stderr.read1(4096):
                        with self.stderr_lock: self.stderr_tail=(self.stderr_tail+chunk)[-8192:]
                except (OSError,ValueError): pass
            self.reader=threading.Thread(target=read,daemon=True); self.reader.start()
            self.stderr_reader=threading.Thread(target=drain_stderr,daemon=True); self.stderr_reader.start()
            self.initialize=self.request('initialize',{'protocolVersion':'2025-06-18','capabilities':{},
                'clientInfo':{'name':'claude-workflow-client','version':'3.0.0'}},startup_timeout)
            self.notify('notifications/initialized',{})
        except BaseException:
            self.close()
            raise

    def send(self,message):
        self.process.stdin.write((json.dumps(message,ensure_ascii=False)+'\n').encode('utf-8'))
        self.process.stdin.flush()

    def notify(self,method,params): self.send({'jsonrpc':'2.0','method':method,'params':params})

    def request(self,method,params,timeout=660):
        self.next_id+=1; rid=self.next_id
        if method=='tools/call': params={**params,'_meta':{**params.get('_meta',{}),'progressToken':rid}}
        self.send({'jsonrpc':'2.0','id':rid,'method':method,'params':params})
        deadline=time.monotonic()+timeout
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                try: self.notify('notifications/cancelled',{'requestId':rid})
                except (OSError,ValueError): pass
                raise TimeoutError(f'MCP request exceeded total deadline of {timeout} seconds.')
            try: message=self.messages.get(timeout=remaining)
            except queue.Empty:
                try: self.notify('notifications/cancelled',{'requestId':rid})
                except (OSError,ValueError): pass
                raise TimeoutError(f'MCP request exceeded total deadline of {timeout} seconds.')
            if isinstance(message,Exception): raise message
            if message.get('method')=='notifications/progress':
                update=message.get('params',{})
                if update.get('progressToken')==rid:
                    if hasattr(self,'progress_events'): self.progress_events.append(update)
                    callback=getattr(self,'on_progress',None)
                    if callback: callback(update)
                continue
            if message.get('id')==rid:
                if 'error' in message: raise RuntimeError(message['error'])
                return message['result']

    def call(self,name,arguments):
        if not isinstance(arguments,dict): raise ValueError('Tool arguments must be an object.')
        timeout=arguments.get('timeout_seconds',180)
        if type(timeout) is not int or not 10<=timeout<=600: raise ValueError('Invalid timeout_seconds.')
        limit=timeout+15 if name!='claude_status' else 135 if arguments.get('live_probe') else 45
        return self.request('tools/call',{'name':name,'arguments':arguments},limit)

    def close(self):
        if self.closed: return
        self.closed=True
        if self.process is None: return
        if self.process.poll() is None:
            try: self.process.stdin.close()
            except (OSError,ValueError): pass
            try: self.process.wait(timeout=2)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=5)
        for reader,stream in ((self.reader,self.process.stdout),(self.stderr_reader,self.process.stderr)):
            if reader: reader.join(timeout=1)
            if not reader or not reader.is_alive(): stream.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool',required=True,choices=['claude_status','ask_claude','review_diff','diagnose_failure'])
    parser.add_argument('--arguments-file',help='UTF-8 JSON file; otherwise read JSON from stdin.')
    parser.add_argument('--output',help='Optional UTF-8 JSON result file.')
    parser.add_argument('--quiet-progress',action='store_true',help='Suppress stage messages on stderr.')
    options=parser.parse_args()
    client=None
    try:
        source=Path(options.arguments_file).read_text(encoding='utf-8-sig') if options.arguments_file else sys.stdin.read()
        arguments=json.loads(source)
        def progress(update):
            if not options.quiet_progress: print('[Claude MCP] '+str(update.get('message',''))[:1000],file=sys.stderr,flush=True)
        client=Client(on_progress=progress); result=client.call(options.tool,arguments)
    except Exception as exc:
        result={'isError':True,'content':[{'type':'text','text':str(exc)}]}
    finally:
        if client: client.close()
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if options.output: Path(options.output).write_text(text,encoding='utf-8')
    print(text)
    return 1 if result.get('isError') else 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
