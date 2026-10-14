"""Bounded in-memory caches. Never persist source or reviews to disk."""
from collections import OrderedDict
from copy import deepcopy
import threading
import time


class Cache:
    def __init__(self,capacity=16,ttl=300):
        self.capacity=capacity; self.ttl=ttl; self.values=OrderedDict(); self.lock=threading.Lock()

    def get(self,key):
        with self.lock:
            entry=self.values.get(key)
            if entry is None: return None
            saved,value=entry; age=time.monotonic()-saved
            if age>self.ttl: self.values.pop(key); return None
            self.values.move_to_end(key)
            return deepcopy(value),age

    def put(self,key,value):
        with self.lock:
            self.values[key]=(time.monotonic(),deepcopy(value)); self.values.move_to_end(key)
            while len(self.values)>self.capacity: self.values.popitem(last=False)

    def clear(self):
        with self.lock: self.values.clear()


SOURCE_CACHE=Cache(32)
REVIEW_CACHE=Cache(16)
