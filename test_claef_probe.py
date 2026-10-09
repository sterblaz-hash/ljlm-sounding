#!/usr/bin/env python3
"""Offline tests: small real GeoJSON fixtures plus mocked HTTP responses."""
import copy
from email.message import Message
import io
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from probe_claef import (BoundedHeader, Client, ProbeError, parameter_inventory,
                         parse_listing, parse_point, percentile_groups, point_url, main)

FIXTURES = Path(__file__).resolve().parent / 'claef/probe'


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.det = json.loads((FIXTURES / 'deterministic_sample.json').read_text())
        self.ens = json.loads((FIXTURES / 'ensemble_sample.json').read_text())
        self.names = list(self.det['features'][0]['properties']['parameters'])
        self.meta = {'last_forecast_reftime': '2026-10-09T00:00+00:00',
                     'parameters': [{'name': '2t', 'long_name': '2m temperature',
                                     'desc': 'air temperature', 'unit': 'degree Celsius'}]}

    def test_actual_point_shape_coordinates_leads_units_and_nulls(self):
        point = parse_point(self.det, self.names)
        self.assertAlmostEqual(point['grid_latitude'], 46.062)
        self.assertAlmostEqual(point['grid_longitude'], 14.5087)
        self.assertEqual([row['lead_hours'] for row in point['hours']], [0, 1, 2, 3])
        self.assertEqual(point['units']['msl'], 'Pa')
        self.assertIsNone(point['hours'][0]['values']['tp'])
        self.assertEqual(point['hours'][1]['values']['2t'], 14.7)

    def test_response_cycle_is_authoritative(self):
        self.det['reference_time'] = '2026-10-08T21:00+00:00'
        self.assertEqual(parse_point(self.det, self.names)['hours'][0]['lead_hours'], 3)

    def test_percentiles_are_separate_parameters(self):
        names = list(self.ens['features'][0]['properties']['parameters'])
        point = parse_point(self.ens, names)
        self.assertEqual(percentile_groups(names)['2t'],
                         {'P10': '2t_p10', 'P50': '2t_p50', 'P90': '2t_p90'})
        self.assertEqual(point['hours'][1]['values']['2t_p90'], 15.0)
        self.assertIsNone(point['hours'][0]['values']['tp_p50'])
        self.assertEqual(percentile_groups(['2t', '2t_p99']), {})

    def test_unknown_parameters_rejected_before_network(self):
        with self.assertRaises(ProbeError):
            point_url('nwp-v2-1h-1km', self.meta, ['dewpoint'])
        with self.assertRaises(ProbeError):
            point_url('nwp-v2-1h-1km', self.meta, [])

    def test_bounded_inclusive_request(self):
        query = parse_qs(urlparse(point_url('nwp-v2-1h-1km', self.meta, ['2t'])).query)
        self.assertEqual(query['lat_lon'], ['46.0656,14.5122'])
        self.assertEqual(query['forecast_offset'], ['0'])
        self.assertEqual(query['end'], ['2026-10-09T03:00:00+00:00'])
        with self.assertRaises(ProbeError):
            point_url('x', self.meta, ['2t'], hours=60)

    def test_invalid_inventory(self):
        for meta in ({}, {'parameters': [{}]}, {'parameters': [{'name': 't'}, {'name': 't'}]}):
            with self.subTest(meta=meta), self.assertRaises(ProbeError):
                parameter_inventory(meta)

    def test_invalid_points(self):
        for mutation in ('length', 'missing', 'time', 'unit', 'nonfinite', 'geometry'):
            data = copy.deepcopy(self.det)
            params = data['features'][0]['properties']['parameters']
            if mutation == 'length': params['2t']['data'].pop()
            elif mutation == 'missing': del params['2t']
            elif mutation == 'time': data['timestamps'][1] = data['timestamps'][0]
            elif mutation == 'unit': params['2t']['unit'] = ''
            elif mutation == 'nonfinite': params['2t']['data'][0] = float('nan')
            elif mutation == 'geometry': data['features'][0]['geometry']['type'] = 'Polygon'
            with self.subTest(mutation=mutation), self.assertRaises(ProbeError):
                parse_point(data, self.names)

    @patch('probe_claef.urlopen')
    def test_timeout_user_agent_and_success(self, open_url):
        response = io.BytesIO(b'{"ok":true}')
        response.headers = {}
        open_url.return_value = response
        self.assertEqual(Client().json('https://example.test')[0], {'ok': True})
        request = open_url.call_args.args[0]
        self.assertIn('LJLM-CLAEF-Probe', request.get_header('User-agent'))
        self.assertEqual(open_url.call_args.kwargs['timeout'], 15)

    @patch('probe_claef.urlopen')
    def test_http_429_stops_without_retry(self, open_url):
        headers = Message(); headers['Retry-After'] = '60'
        open_url.side_effect = HTTPError('https://example.test', 429, 'limited', headers, None)
        with self.assertRaisesRegex(ProbeError, '429.*60'):
            Client().json('https://example.test')
        self.assertEqual(open_url.call_count, 1)

    @patch('probe_claef.urlopen')
    def test_http_network_and_invalid_json_errors(self, open_url):
        for error in (HTTPError('x', 503, 'Unavailable', Message(), None), URLError('DNS'), TimeoutError()):
            open_url.side_effect = error
            with self.assertRaises(ProbeError): Client().json('https://example.test')
        open_url.side_effect = None
        response = io.BytesIO(b'not json'); response.headers = {}
        open_url.return_value = response
        with self.assertRaisesRegex(ProbeError, 'invalid JSON'): Client().json('https://example.test')

    @patch('probe_claef.urlopen')
    def test_bulk_refuses_ignored_or_invalid_range(self, open_url):
        for status, headers in ((200, {}), (206, {'Content-Range': 'bytes 0-5/100'})):
            response = io.BytesIO(b'1234'); response.status = status; response.headers = headers
            open_url.return_value = response
            with self.assertRaises(ProbeError): Client().read('https://example.test', byte_range=4)
        response = io.BytesIO(b'1234'); response.status = 206
        response.headers = {'Content-Range': 'bytes 0-3/100'}
        open_url.return_value = response
        self.assertEqual(Client().read('https://example.test', byte_range=4)[0], b'1234')

    @patch('probe_claef.MAX_BYTES', 3)
    @patch('probe_claef.urlopen')
    def test_response_size_cap(self, open_url):
        response = io.BytesIO(b'1234'); response.headers = {}
        open_url.return_value = response
        with self.assertRaisesRegex(ProbeError, 'exceeded'): Client().read('https://example.test')

    @patch('probe_claef.time.sleep')
    @patch('probe_claef.time.monotonic', side_effect=[10, 10.2, 10.2])
    @patch('probe_claef.urlopen')
    def test_request_pacing(self, open_url, monotonic, sleep):
        responses = []
        for _ in range(2):
            response = io.BytesIO(b'{}'); response.headers = {}; responses.append(response)
        open_url.side_effect = responses
        client = Client(); client.json('https://example.test'); client.json('https://example.test')
        self.assertAlmostEqual(sleep.call_args.args[0], .8)

    def test_bounded_header_never_substitutes_missing_bytes(self):
        header = BoundedHeader(b'abcd', 1000000000)
        self.assertEqual(header.seek(0, 2), 1000000000)
        header.seek(0)
        self.assertEqual(header.read(4), b'abcd')
        with self.assertRaises(OSError): header.read(1)

    @patch('probe_claef.Client.json')
    def test_complete_default_probe_is_four_requests_and_preserves_samples(self, fetch):
        metas = []
        for sample in (self.det, self.ens):
            parameters = sample['features'][0]['properties']['parameters']
            metas.append({'last_forecast_reftime': sample['reference_time'],
                          'parameters': [{'name': name, 'unit': item['unit']}
                                         for name, item in parameters.items()]})
        fetch.side_effect = [(metas[0], 100), (self.det, 1545),
                             (metas[1], 200), (self.ens, 1456)]
        with tempfile.TemporaryDirectory() as temp, patch('builtins.print'):
            self.assertEqual(main(['--output', temp]), 0)
            self.assertEqual(json.loads((Path(temp) / 'deterministic_sample.json').read_text()), self.det)
            self.assertEqual(json.loads((Path(temp) / 'ensemble_sample.json').read_text()), self.ens)
            self.assertTrue((Path(temp) / 'inventory.md').exists())
        self.assertEqual(fetch.call_count, 4)
        for call in fetch.call_args_list:
            self.assertIn('/timeseries/forecast/', call.args[0])

    def test_listing_and_truncation(self):
        xml = b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated><Contents><Key>x.nc</Key><Size>9000000000</Size><ETag>abc</ETag></Contents></ListBucketResult>'
        self.assertEqual(parse_listing(xml)[0]['bytes'], 9000000000)
        with self.assertRaises(ProbeError): parse_listing(xml.replace(b'false', b'true'))


if __name__ == '__main__':
    unittest.main()
