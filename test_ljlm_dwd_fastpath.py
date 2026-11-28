"""Network-free regression tests for the first DWD reliability patch."""

from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import dwd_transfer
import ljlm_dwd_fastpath as fast


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class DownloadTests(unittest.TestCase):
    def test_success(self):
        seen = []
        def opener(req, timeout):
            seen.append((req.full_url, timeout))
            return Response(b'BUFR-data', {'Content-Length': '9'})
        got = dwd_transfer.download_dwd('https://opendata.dwd.de/test', opener=opener)
        self.assertEqual(got, b'BUFR-data')
        self.assertEqual(seen, [('https://opendata.dwd.de/test', 30)])

    def test_transient_503_then_success(self):
        attempts = []
        sleeps = []
        def opener(req, timeout):
            attempts.append(1)
            if len(attempts) == 1:
                raise HTTPError(req.full_url, 503, 'unavailable', {}, None)
            return Response(b'ok')
        self.assertEqual(dwd_transfer.download_dwd('https://dwd.example/x',
                         opener=opener, sleeper=sleeps.append, logger=lambda _:None), b'ok')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [2])

    def test_network_timeout_then_success(self):
        attempts = []
        def opener(req, timeout):
            attempts.append(1)
            if len(attempts) < 3:
                raise URLError(TimeoutError('timeout'))
            return Response(b'ok')
        sleeps = []
        self.assertEqual(dwd_transfer.download_dwd('https://dwd.example/x',
                         opener=opener, sleeper=sleeps.append, logger=lambda _:None), b'ok')
        self.assertEqual(sleeps, [2, 6])

    def test_404_never_retried(self):
        calls = []
        def opener(req, timeout):
            calls.append(1)
            raise HTTPError(req.full_url, 404, 'missing', {}, None)
        with self.assertRaises(HTTPError):
            dwd_transfer.download_dwd('https://dwd.example/x',opener=opener,
                                       sleeper=lambda t:self.fail('unexpected sleep'))
        self.assertEqual(len(calls), 1)

    def test_429_honors_bounded_retry_after(self):
        calls = []
        sleeps = []
        def opener(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError(req.full_url, 429, 'rate limit',
                                {'Retry-After':'10'}, None)
            return Response(b'ok')
        self.assertEqual(dwd_transfer.download_dwd('https://dwd.example/x',
                         opener=opener,sleeper=sleeps.append, logger=lambda _:None), b'ok')
        self.assertEqual(sleeps, [10])


    def test_long_retry_after_respected_and_not_retried(self):
        calls = []
        def opener(req, timeout):
            calls.append(1)
            raise HTTPError(req.full_url, 429, 'rate limit',
                            {'Retry-After':'9999'}, None)
        with self.assertRaises(HTTPError):
            dwd_transfer.download_dwd('https://dwd.example/x',opener=opener,
                    sleeper=lambda _:self.fail('server said to wait'),logger=lambda _:None)
        self.assertEqual(len(calls),1)

    def test_exhausted_timeout_propagates(self):
        calls = []
        def opener(req, timeout):
            calls.append(1)
            raise TimeoutError('timed out')
        with self.assertRaises(TimeoutError):
            dwd_transfer.download_dwd('https://dwd.example/x',opener=opener,
                sleeper=lambda _:None,logger=lambda _:None)
        self.assertEqual(len(calls),3)

    def test_predeclared_large_response_refused(self):
        def opener(req, timeout):
            return Response(b'',{'Content-Length':'500'})
        with self.assertRaises(ValueError):
            dwd_transfer.download_dwd('https://dwd.example/x',opener=opener,max_bytes=4)

    def test_unannounced_large_response_refused(self):
        def opener(req, timeout):
            return Response(b'12345')
        with self.assertRaises(ValueError):
            dwd_transfer.download_dwd('https://dwd.example/x',opener=opener,max_bytes=4)


class CachedPackageTests(unittest.TestCase):
    def test_recent_positive_skip_only_if_complete(self):
        now = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
        cache = {'contains_ljlm':True,'scanned_at':(now-timedelta(minutes=10)).isoformat()}
        self.assertTrue(fast.can_skip_recent_positive(cache,now,True))
        self.assertFalse(fast.can_skip_recent_positive(cache,now,False))
        self.assertFalse(fast.can_skip_recent_positive({**cache,'contains_ljlm':False},now,True))
        self.assertFalse(fast.can_skip_recent_positive({**cache,'scanned_at':(now-timedelta(minutes=21)).isoformat()},now,True))
        self.assertFalse(fast.can_skip_recent_positive({**cache,'scanned_at':(now+timedelta(minutes=1)).isoformat()},now,True))

    def test_corrupt_cache_never_suppresses_work(self):
        self.assertFalse(fast.can_skip_recent_positive({'contains_ljlm':True,'scanned_at':'garbage'},
                     datetime.now(timezone.utc),True))


class LatestGuardTests(unittest.TestCase):
    def mk(self,ts,levels=1200):
        return {'launch_time':ts,'qc':{'profile_level_count':levels}}

    def test_older_never_overwrites_newer(self):
        a=self.mk('2026-11-02T11:30:00Z')
        b=self.mk('2026-11-02T23:30:00Z')
        self.assertFalse(fast.latest_may_advance(a,b))
        self.assertTrue(fast.latest_may_advance(b,a))

    def test_same_launch_better_and_worse(self):
        a=self.mk('2026-11-02T11:30:00Z',1700)
        b=self.mk('2026-11-02T11:30:00Z',2000)
        self.assertFalse(fast.latest_may_advance(a,b))
        self.assertTrue(fast.latest_may_advance(b,a))
        self.assertTrue(fast.latest_may_advance(b,b))

    def test_corrupt_launch_does_not_advance(self):
        self.assertFalse(fast.latest_may_advance({'launch_time':'garbage'},self.mk('2026-11-02T11:30:00Z')))

    def test_marker_fail_safe(self):
        with tempfile.TemporaryDirectory() as d:
            marker=Path(d)/'.ljlm_observations_updated'
            with patch.object(fast,'MARKER_FILE',marker):
                self.assertTrue(fast.marker_has_updates())  # No marker => full pipeline
                fast.write_marker(0)
                self.assertFalse(fast.marker_has_updates())
                fast.write_marker(1)
                self.assertTrue(fast.marker_has_updates())
                marker.write_text('corrupt')
                self.assertTrue(fast.marker_has_updates())


if __name__ == '__main__':
    unittest.main()
