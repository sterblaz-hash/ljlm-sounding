"""Offline resilience tests: no external network requests and no sleeping."""
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import claef_pressure_profile as p


class TestTransferRetry(unittest.TestCase):
    URL = 'https://example.test/datahub/nwp_2026102900.nc'
    TOTAL = 9_000_000_000
    ETAG = 'abc'

    @staticmethod
    def response(body=b'abcd', *, status=206, content_range=None, etag='"abc"'):
        resp = io.BytesIO(body)
        resp.status = status
        resp.headers = {'Content-Range': content_range or 'bytes 100-103/9000000000',
                        'ETag': etag, 'Content-Length': str(len(body))}
        return resp

    @patch('claef_pressure_profile.time.sleep')
    @patch('claef_pressure_profile.urlopen')
    def test_timeout_retries_same_range(self, fetch, sleep):
        fetch.side_effect = [URLError('timed out'), self.response()]
        client = p.TransferClient()
        self.assertEqual(client.get(self.URL, 100, 103, self.TOTAL, self.ETAG), b'abcd')
        self.assertEqual(client.requests, 2)
        self.assertEqual(client.bytes, 4)
        self.assertEqual(len(client.range_log), 1)
        self.assertEqual([c.args[0].get_header('Range') for c in fetch.call_args_list],
                         ['bytes=100-103', 'bytes=100-103'])
        self.assertEqual(fetch.call_args.kwargs['timeout'], 30)

    @patch('claef_pressure_profile.time.sleep')
    @patch('claef_pressure_profile.urlopen')
    def test_partial_body_before_timeout_counts_budget(self, fetch, sleep):
        class FlakyBody(io.BytesIO):
            def __init__(self):
                super().__init__(b'abcd')
                self.status = 206
                self.headers = {'Content-Range': 'bytes 100-103/9000000000',
                                'ETag': '"abc"', 'Content-Length': '4'}
                self.reads = 0

            def read(self, n=-1):
                self.reads += 1
                if self.reads == 1:
                    return super().read(2)
                raise TimeoutError('timeout mid-body')
        fetch.side_effect = [FlakyBody(), self.response()]
        client = p.TransferClient()
        self.assertEqual(client.get(self.URL, 100, 103, self.TOTAL, self.ETAG), b'abcd')
        self.assertEqual(client.bytes, 6)
        self.assertEqual(client.requests, 2)
        self.assertEqual(client.range_log, [{'start': 100, 'end': 103, 'bytes': 4}])

    @patch('claef_pressure_profile.time.sleep')
    @patch('claef_pressure_profile.urlopen')
    def test_three_timeouts_report_range_and_request(self, fetch, sleep):
        fetch.side_effect = URLError('timed out')
        client = p.TransferClient()
        with self.assertRaisesRegex(p.ProbeError, r'range=100-103.*request #3'):
            client.get(self.URL, 100, 103, self.TOTAL, self.ETAG)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(client.requests, 3)

    @patch('claef_pressure_profile.time.sleep')
    @patch('claef_pressure_profile.urlopen')
    def test_503_retries_but_429_does_not(self, fetch, sleep):
        fetch.side_effect = [HTTPError(self.URL, 503, 'unavailable', {}, None), self.response()]
        self.assertEqual(p.TransferClient().get(self.URL, 100, 103, self.TOTAL, self.ETAG), b'abcd')
        self.assertEqual(fetch.call_count, 2)
        fetch.reset_mock()
        fetch.side_effect = HTTPError(self.URL, 429, 'rate limited', {}, None)
        with self.assertRaisesRegex(p.ProbeError, '429'):
            p.TransferClient().get(self.URL, 100, 103, self.TOTAL, self.ETAG)
        self.assertEqual(fetch.call_count, 1)

    @patch('claef_pressure_profile.time.sleep')
    @patch('claef_pressure_profile.urlopen')
    def test_etag_or_range_violation_never_retries_or_reads(self, fetch, sleep):
        for response in (self.response(etag='"changed"'),
                         self.response(content_range='bytes 100-104/9000000000')):
            response.read = lambda n=-1: self.fail('Must reject before reading body')
            fetch.reset_mock()
            fetch.side_effect = None
            fetch.return_value = response
            with self.assertRaises(p.SafetyRefusal):
                p.TransferClient().get(self.URL, 100, 103, self.TOTAL, self.ETAG)
            self.assertEqual(fetch.call_count, 1)

    @patch('claef_pressure_profile.time.sleep')
    @patch('claef_pressure_profile.urlopen')
    def test_budget_includes_partial_failed_attempt(self, fetch, sleep):
        class Partial(io.BytesIO):
            def __init__(self):
                super().__init__(b'abcd')
                self.status = 206
                self.headers = {'Content-Range': 'bytes 100-103/9000000000',
                                'ETag': '"abc"', 'Content-Length': '4'}
                self.reads = 0

            def read(self, n=-1):
                self.reads += 1
                if self.reads == 1: return super().read(2)
                raise TimeoutError('interrupted')
        fetch.return_value = Partial()
        client = p.TransferClient()
        client.bytes = p.MAX_TOTAL - 4
        with self.assertRaisesRegex(p.SafetyRefusal, 'budget exceeded'):
            client.get(self.URL, 100, 103, self.TOTAL, self.ETAG)
        self.assertEqual(client.bytes, p.MAX_TOTAL - 2)
        self.assertEqual(fetch.call_count, 1)

    @patch('claef_pressure_profile.time.sleep')
    @patch('claef_pressure_profile.urlopen')
    def test_request_budget_counts_failures(self, fetch, sleep):
        fetch.side_effect = URLError('timed out')
        client = p.TransferClient()
        client.requests = p.MAX_REQUESTS - 1
        with self.assertRaisesRegex(p.SafetyRefusal, 'budget exceeded'):
            client.get(self.URL, 100, 103, self.TOTAL, self.ETAG)
        self.assertEqual(client.requests, p.MAX_REQUESTS)
        self.assertEqual(fetch.call_count, 1)

    @patch('claef_pressure_profile.urlopen')
    def test_http_200_range_refused_without_body(self, fetch):
        response = self.response(status=200)
        response.read = lambda n=-1: self.fail('Unbounded body must not be read')
        fetch.return_value = response
        with self.assertRaises(p.SafetyRefusal):
            p.TransferClient().get(self.URL, 100, 103, self.TOTAL, self.ETAG)
        self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
