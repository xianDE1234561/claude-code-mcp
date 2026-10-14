import time
import unittest
from unittest.mock import patch
from volatile_cache import Cache
from workflow_progress import Progress


class UtilityTests(unittest.TestCase):
    def test_cache_expiration_and_lru(self):
        cache=Cache(2,ttl=5)
        with patch('volatile_cache.time.monotonic',return_value=10):
            cache.put('a',{'v':1}); cache.put('b',{'v':2}); cache.get('a'); cache.put('c',{'v':3})
            self.assertIsNone(cache.get('b'))
        with patch('volatile_cache.time.monotonic',return_value=16): self.assertIsNone(cache.get('a'))

    def test_cached_values_cannot_be_mutated_by_callers(self):
        cache=Cache(); value={'items':[1]}; cache.put('a',value); value['items'].append(2)
        received,_=cache.get('a'); received['items'].append(3)
        self.assertEqual(cache.get('a')[0],{'items':[1]})

    def test_progress_heartbeat_stops_before_completion(self):
        events=[]; progress=Progress(events.append,interval=.01); progress.set('request_model')
        time.sleep(.04); summary=progress.summary(); count=len(events); time.sleep(.03)
        self.assertGreater(count,1); self.assertEqual(count,len(events))
        self.assertEqual([e['progress'] for e in events],list(range(1,count+1)))
        self.assertTrue(all('total' not in e for e in events)); self.assertIn('request_model',summary['stages_seconds'])


if __name__=='__main__': unittest.main()
