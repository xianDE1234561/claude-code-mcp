"""Bounded NDJSON observation. Never forward raw text, thinking, IDs or paths."""
import json
import time
import tempfile
from contextlib import contextmanager


@contextmanager
def capture_directory():
    directory=tempfile.TemporaryDirectory(prefix='claude-mcp-')
    failure=None
    try: yield directory.name
    except BaseException as exc:
        failure=exc
        raise
    finally:
        # Windows can briefly retain inherited file handles after process exit.
        for attempt in range(51):
            try: directory.cleanup(); break
            except PermissionError:
                if attempt==50:
                    if failure is not None:
                        failure.capture_cleanup_failed=True
                        break
                    raise
                time.sleep(.05)

ERRORS={'authentication_failed','oauth_org_not_allowed','account_on_hold','billing_error',
        'rate_limit','overloaded','invalid_request','model_not_found','server_error',
        'max_output_tokens','cloud_credential_error','unknown'}


def integer(value,low,high):
    return value if type(value) is int and low<=value<=high else None


class Observer:
    def __init__(self,callback=None):
        self.callback=callback; self.started=time.monotonic(); self.buffer=b''; self.total_bytes=0
        self.result=None; self.initialized=False; self.first_event=None; self.last_event=None
        self.counts={}; self.events=[]; self.dropped=0; self.last_activity_notice=0
        self.last_keepalive_notice=0

    def feed(self,data):
        self.total_bytes+=len(data)
        if self.total_bytes>2_000_000: raise ValueError('CLI stream exceeds total capture limit.')
        self.buffer+=data
        while b'\n' in self.buffer:
            line,self.buffer=self.buffer.split(b'\n',1)
            if len(line)>250000: raise ValueError('CLI stream event exceeds capture limit.')
            if line.strip(): self.consume(json.loads(line))
        if len(self.buffer)>250000: raise ValueError('CLI stream event exceeds capture limit.')

    def finish(self):
        if self.buffer.strip(): self.consume(json.loads(self.buffer))
        self.buffer=b''

    def emit(self,kind,fields=None):
        elapsed=round(time.monotonic()-self.started,3)
        self.first_event=elapsed if self.first_event is None else self.first_event
        self.last_event=elapsed; self.counts[kind]=self.counts.get(kind,0)+1
        event={'kind':kind,'elapsed_seconds':elapsed,**(fields or {})}
        if len(self.events)<120: self.events.append(event)
        else: self.dropped+=1
        if self.callback: self.callback(event)

    def consume(self,event):
        if not isinstance(event,dict): raise ValueError('CLI stream event must be an object.')
        kind=event.get('type'); subtype=event.get('subtype')
        if kind=='system' and subtype=='init':
            self.initialized=True; self.emit('initialized')
        elif kind=='system' and subtype=='api_retry':
            fields={key:integer(event.get(key),low,high) for key,low,high in
                    [('attempt',0,1000),('max_retries',0,1000),('retry_delay_ms',0,600000),('error_status',100,599)]}
            fields={k:v for k,v in fields.items() if v is not None}
            category=event.get('error')
            fields['error_category']=category if isinstance(category,str) and category in ERRORS else 'unknown'
            self.emit('api_retry',fields)
        elif kind=='stream_event':
            payload=event.get('event',{})
            if not isinstance(payload,dict): return
            if payload.get('type')=='ping':
                self.counts['transport_keepalive_events']=self.counts.get('transport_keepalive_events',0)+1
                now=time.monotonic()
                if now-self.last_keepalive_notice>=1:
                    self.last_keepalive_notice=now; self.emit('transport_keepalive')
                return
            delta=payload.get('delta',{})
            if payload.get('type')!='content_block_delta' or not isinstance(delta,dict): return
            content_kind={'text_delta':'text','thinking_delta':'thinking','input_json_delta':'structured_json'}.get(delta.get('type'))
            if content_kind is None: return
            self.activity(content_kind)
        elif kind=='assistant':
            message=event.get('message',{})
            content=message.get('content',[]) if isinstance(message,dict) else []
            if isinstance(content,list) and any(isinstance(block,dict) and block.get('type') in ('text','thinking','tool_use') for block in content):
                self.activity('assistant_message')
        elif kind=='result':
            self.result=event; self.emit('final_result',{'is_error':event.get('is_error') is True})
        # Unknown events and all their payloads are ignored.

    def activity(self,content_kind):
            now=time.monotonic()
            self.counts['output_events']=self.counts.get('output_events',0)+1
            if now-self.last_activity_notice>=1:
                self.last_activity_notice=now; self.emit('output_activity',{'content_kind':content_kind})

    def summary(self):
        return {'initialized':self.initialized,'final_result_received':self.result is not None,
                'first_event_seconds':self.first_event,'last_event_seconds':self.last_event,
                'event_counts':dict(self.counts),'events':list(self.events),'dropped_events':self.dropped}
