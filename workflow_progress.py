"""Stage timing plus optional MCP progress notifications, without percentages."""
import threading
import time

LABELS={'collect_context':'收集上下文','request_model':'等待 Claude','validate_result':'校验结果',
        'completed':'完成','failed':'失败','no_changes':'无改动','cache_hit':'读取审查缓存'}


class Progress:
    def __init__(self,callback=None,interval=5):
        self.callback=callback; self.interval=interval; self.started=time.monotonic()
        self.changed=self.started; self.stage=None; self.counter=0
        self.timings={}; self.events=[]; self.lock=threading.Lock(); self.stopped=threading.Event()
        self.thread=None
        if callback:
            self.thread=threading.Thread(target=self._heartbeat,daemon=True); self.thread.start()

    def _emit(self):
        if not self.callback or not self.stage: return
        self.counter+=1
        elapsed=round(time.monotonic()-self.started,1)
        self.callback({'progress':self.counter,'message':f'{LABELS.get(self.stage,self.stage)} · 已用 {elapsed}s'})

    def set(self,stage):
        with self.lock:
            if self.stopped.is_set(): return
            now=time.monotonic()
            if self.stage:
                self.timings[self.stage]=self.timings.get(self.stage,0)+now-self.changed
            self.stage=stage; self.changed=now
            self.events.append({'stage':stage,'elapsed_seconds':round(now-self.started,3)})
            self._emit()

    def _heartbeat(self):
        while not self.stopped.wait(self.interval):
            with self.lock:
                if not self.stopped.is_set(): self._emit()

    def close(self):
        with self.lock:
            if not self.stopped.is_set():
                now=time.monotonic()
                if self.stage: self.timings[self.stage]=self.timings.get(self.stage,0)+now-self.changed
                self.total=now-self.started; self.stopped.set()
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(timeout=1)

    def summary(self):
        self.close()
        return {'total_seconds':round(self.total,3),
                'stages_seconds':{k:round(v,3) for k,v in self.timings.items()},'events':list(self.events)}
