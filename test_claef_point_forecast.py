#!/usr/bin/env python3
"""Offline tests using validated small probe fixtures and mocked transport."""
from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import claef_point_forecast as c

FIXTURES = Path(__file__).resolve().parent / 'claef/probe'
CYCLE = '2026-10-09T00:00:00Z'
NOW = datetime(2026, 10, 9, 6, tzinfo=timezone.utc)


def fixtures():
    responses = {kind: json.loads((FIXTURES / (kind + '_sample.json')).read_text()) for kind in c.RESOURCES}
    metadata = {}
    for kind, response in responses.items():
        parameters = response['features'][0]['properties']['parameters']
        metadata[kind] = {
            'last_forecast_reftime': response['reference_time'],
            'available_forecast_reftimes': [response['reference_time'], '2026-10-08T21:00+00:00'],
            'max_forecast_offset': 1, 'forecast_length': 4, 'frequency': '1H',
            'parameters': [{'name': name, 'long_name': item['name'], 'desc': item['name'], 'unit': item['unit']}
                           for name, item in parameters.items()]}
    return metadata, responses


def client_for(metadata, responses):
    client = Mock()
    client.json.side_effect = [metadata['deterministic'], metadata['ensemble'],
                               responses['deterministic'], responses['ensemble']]
    return client


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.metadata, self.responses = fixtures()

    def build(self):
        return c.build_product(self.metadata, self.responses, CYCLE, NOW.isoformat())

    def test_deterministic_native_units_values_and_grid(self):
        product = self.build()
        self.assertEqual(product['requested_location'], {'latitude': 46.0656, 'longitude': 14.5122})
        self.assertAlmostEqual(product['sources']['deterministic']['grid_point']['latitude'], 46.062)
        self.assertAlmostEqual(product['sources']['ensemble']['grid_point']['longitude'], 14.5087)
        self.assertEqual(product['sources']['deterministic']['dataset'], 'nwp-v2-1h-1km')
        self.assertEqual(product['sources']['ensemble']['dataset'], 'ensemble-v2-1h-1km')
        self.assertEqual(product['parameters']['deterministic']['msl']['unit'], 'Pa')
        self.assertEqual(product['records'][1]['deterministic']['2t'], 14.7)
        self.assertEqual(product['records'][1]['deterministic']['tp'], .016)
        self.assertEqual(product['records'][1]['deterministic']['10u'], 1.1)
        self.assertEqual([r['lead_hours'] for r in product['records']], [0, 1, 2, 3])

    def test_separate_percentiles_and_missing_inventory(self):
        product = self.build()
        self.assertEqual(product['records'][1]['ensemble']['2t'], {'P10': 13.4, 'P50': 14.6, 'P90': 15.0})
        self.assertNotIn('sf', product['records'][1]['ensemble'])
        self.assertIn('sf_p10', product['sources']['ensemble']['unavailable_requested_parameters'])
        self.assertEqual(product['parameters']['ensemble']['2t_p50']['unit'], 'degree Celsius')

    def test_nulls_interval_semantics_and_zero_are_distinct(self):
        product = self.build()
        self.assertIsNone(product['records'][0]['deterministic']['tp'])
        self.assertIsNone(product['records'][0]['ensemble']['tp']['P90'])
        self.assertEqual(product['records'][2]['deterministic']['tp'], 0)
        self.assertIsNone(product['records'][0]['interval_start'])
        self.assertEqual(product['records'][1]['interval_start'], CYCLE)
        self.assertEqual(product['parameters']['deterministic']['sf']['temporal_semantics'], 'interval_amount')
        self.assertIn('no conversion', product['parameters']['deterministic']['sf']['interpretation'])
        self.assertIn('not cumulative', product['parameters']['deterministic']['tp']['interval'])

    def test_valid_time_join_uses_instants_not_array_positions(self):
        response = self.responses['ensemble']
        response['timestamps'] = ['2026-10-09T01:00Z', '2026-10-09T03:00Z']
        for parameter in response['features'][0]['properties']['parameters'].values():
            parameter['data'] = [parameter['data'][1], parameter['data'][3]]
        records = self.build()['records']
        self.assertEqual(len(records), 4)
        self.assertEqual(records[1]['ensemble']['2t']['P50'], 14.6)
        self.assertEqual(records[3]['ensemble']['2t']['P50'], 14.2)
        self.assertIsNone(records[2]['ensemble']['2t']['P50'])
        self.assertFalse(records[2]['source_time_available']['ensemble'])
        self.assertTrue(records[2]['source_time_available']['deterministic'])

    def test_all_confirmed_variable_families_and_percentiles(self):
        # Extend tiny fixtures with mock values to exercise all 14/42 names,
        # including rain and sunshine absent from the original small probe.
        for kind in c.RESOURCES:
            params = self.responses[kind]['features'][0]['properties']['parameters']
            names = c.PARAMETERS if kind == 'deterministic' else [b + '_' + p.lower() for b in c.PARAMETERS for p in c.PERCENTILES]
            for name in names:
                if name not in params:
                    params[name] = {'name': name, 'unit': 'mock native unit', 'data': [None, 1, 2, 3]}
                    self.metadata[kind]['parameters'].append({'name': name, 'unit': 'mock native unit'})
        product = self.build()
        self.assertEqual(len(product['parameters']['deterministic']), 14)
        self.assertEqual(len(product['parameters']['ensemble']), 42)
        self.assertEqual(product['records'][1]['deterministic']['rain'], 1)
        self.assertEqual(product['records'][0]['ensemble']['sund'], {'P10': None, 'P50': None, 'P90': None})

    def test_equivalent_timezone_timestamps_join(self):
        self.responses['ensemble']['timestamps'] = [f'2026-10-09T0{i+2}:00+02:00' for i in range(4)]
        self.assertTrue(all(row['source_time_available']['ensemble'] for row in self.build()['records']))

    def test_different_source_grid_points_remain_explicit(self):
        self.responses['ensemble']['features'][0]['geometry']['coordinates'] = [14.52, 46.07]
        product = self.build()
        self.assertEqual(product['sources']['ensemble']['grid_point'], {'latitude': 46.07, 'longitude': 14.52})
        self.assertNotEqual(product['sources']['ensemble']['grid_point'], product['sources']['deterministic']['grid_point'])

    def test_cycle_mismatch_and_unit_mismatch_rejected(self):
        self.responses['ensemble']['reference_time'] = '2026-10-08T21:00Z'
        with self.assertRaisesRegex(c.ProbeError, 'cycle changed'): self.build()
        self.metadata, self.responses = fixtures()
        self.responses['deterministic']['features'][0]['properties']['parameters']['msl']['unit'] = 'hPa'
        with self.assertRaisesRegex(c.ProbeError, 'unit mismatch'): self.build()

    def test_newest_common_cycle_selected_and_offset_explicit(self):
        self.metadata['deterministic']['last_forecast_reftime'] = '2026-10-09T03:00Z'
        self.metadata['deterministic']['available_forecast_reftimes'].insert(0, '2026-10-09T03:00Z')
        self.metadata['deterministic']['max_forecast_offset'] = 2
        cycle = c.select_cycle(self.metadata)
        self.assertEqual(cycle, CYCLE)
        query = parse_qs(urlparse(c.forecast_url('deterministic', self.metadata['deterministic'], cycle, ['2t'])).query)
        self.assertEqual(query['forecast_offset'], ['1'])
        self.assertEqual(query['start'], [CYCLE])
        self.assertEqual(query['end'], ['2026-10-09T03:00:00Z'])
        self.assertEqual(query['lat_lon'], ['46.0656,14.5122'])

    def test_no_common_cycle_unsupported_cadence_and_incomplete_forecast(self):
        self.metadata['ensemble']['last_forecast_reftime'] = '2026-10-08T18:00Z'
        self.metadata['ensemble']['available_forecast_reftimes'] = ['2026-10-08T18:00Z']
        with self.assertRaisesRegex(c.ProbeError, 'No common'): c.select_cycle(self.metadata)
        self.metadata, self.responses = fixtures()
        self.metadata['ensemble']['frequency'] = '3H'
        with self.assertRaisesRegex(c.ProbeError, 'cadence'): c.select_cycle(self.metadata)
        self.metadata, self.responses = fixtures()
        self.metadata['deterministic']['forecast_length'] = 5
        with self.assertRaisesRegex(c.ProbeError, 'coverage'): c.validate_coverage(self.metadata['deterministic'], self.responses['deterministic'], CYCLE, c.request_names('deterministic', self.metadata['deterministic']))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'point.json'
        self.metadata, self.responses = fixtures()

    def write(self):
        return c.refresh(self.output, client_for(self.metadata, self.responses), NOW)

    def test_atomic_output_structure(self):
        result = self.write()
        product = json.loads(self.output.read_text())
        self.assertEqual(result['status'], 'written')
        self.assertEqual(result['forecast_times'], 4)
        self.assertEqual(result['bytes'], self.output.stat().st_size)
        self.assertEqual(product['reference_time'], CYCLE)
        self.assertEqual(product['schema_version'], c.SCHEMA_VERSION)
        self.assertIn('retrieved_at', product)
        self.assertIn('interval_metadata', product)
        self.assertEqual(list(self.output.parent.glob('*.tmp')), [])

    def test_forecast_bounds_and_success_status(self):
        self.write()
        product = json.loads(self.output.read_text())
        self.assertEqual(product['forecast_start'], CYCLE)
        self.assertEqual(product['forecast_end'], '2026-10-09T03:00:00Z')
        self.assertEqual(product['forecast_hour_count'], 4)
        status = json.loads(self.output.with_name('status.json').read_text())
        self.assertEqual(status, {'status': 'ok', 'model_reference_time': CYCLE,
                                 'retrieved_at': product['retrieved_at'], 'forecast_hour_count': 4})
        self.assertNotIn('records', status)

    def test_same_cycle_does_not_change_status(self):
        self.write()
        before = self.output.with_name('status.json').read_bytes()
        client = Mock(); client.json.side_effect = list(self.metadata.values())
        c.refresh(self.output, client, NOW + timedelta(minutes=16))
        self.assertEqual(self.output.with_name('status.json').read_bytes(), before)

    def test_failure_status_preserves_forecast(self):
        self.write(); before = self.output.read_bytes()
        client = Mock(); client.json.side_effect = c.RetrievalError('HTTP 503')
        with self.assertRaises(c.RetrievalError):
            c.refresh(self.output, client, NOW + timedelta(minutes=16))
        status = json.loads(self.output.with_name('status.json').read_text())
        self.assertEqual(status['status'], 'error')
        self.assertEqual(status['model_reference_time'], CYCLE)
        self.assertTrue(status['previous_forecast_preserved'])
        self.assertEqual(self.output.read_bytes(), before)

    def test_local_throttle_makes_no_requests(self):
        self.write()
        client = Mock()
        result = c.refresh(self.output, client, NOW + timedelta(minutes=1))
        self.assertEqual(result['status'], 'throttled')
        client.json.assert_not_called()

    def test_same_cycle_fetches_only_metadata_and_keeps_retrieval_time(self):
        self.write()
        before = self.output.read_bytes()
        client = Mock(); client.json.side_effect = list(self.metadata.values())
        result = c.refresh(self.output, client, NOW + timedelta(minutes=16))
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(client.json.call_count, 2)
        self.assertEqual(self.output.read_bytes(), before)

    def test_changed_cycle_refreshes_all_and_replaces_cache(self):
        self.write()
        for kind in c.RESOURCES:
            self.metadata[kind]['last_forecast_reftime'] = '2026-10-09T03:00Z'
            self.metadata[kind]['available_forecast_reftimes'].insert(0, '2026-10-09T03:00Z')
            self.responses[kind]['reference_time'] = '2026-10-09T03:00Z'
            self.responses[kind]['timestamps'] = [f'2026-10-09T0{i+3}:00Z' for i in range(4)]
        result = c.refresh(self.output, client_for(self.metadata, self.responses), NOW + timedelta(minutes=16))
        self.assertEqual(result['reference_time'], '2026-10-09T03:00:00Z')
        self.assertEqual(json.loads(self.output.read_text())['records'][0]['lead_hours'], 0)

    def test_failures_leave_previous_output_and_gate_retries(self):
        self.write(); before = self.output.read_bytes()
        client = Mock(); client.json.side_effect = c.RetrievalError('HTTP 429', NOW + timedelta(hours=2))
        with self.assertRaises(c.RetrievalError): c.refresh(self.output, client, NOW + timedelta(minutes=16))
        self.assertEqual(self.output.read_bytes(), before)
        client = Mock()
        result = c.refresh(self.output, client, NOW + timedelta(hours=1))
        self.assertEqual(result['status'], 'throttled'); client.json.assert_not_called()

    def test_response_cycle_race_keeps_cache(self):
        self.write(); before = self.output.read_bytes()
        self.responses['ensemble']['reference_time'] = '2026-10-09T03:00Z'
        # Inventory change invalidates same-cycle cache so response validation runs.
        self.metadata['deterministic']['parameters'][0]['desc'] = 'Updated description'
        with self.assertRaises(c.ProbeError): c.refresh(self.output, client_for(self.metadata, self.responses), NOW + timedelta(minutes=16))
        self.assertEqual(self.output.read_bytes(), before)

    def test_refuses_downgrade(self):
        self.write(); before = self.output.read_bytes()
        for meta in self.metadata.values():
            meta['last_forecast_reftime'] = '2026-10-08T21:00Z'
            meta['available_forecast_reftimes'] = ['2026-10-08T21:00Z']
        client = Mock(); client.json.side_effect = list(self.metadata.values())
        with self.assertRaisesRegex(c.ProbeError, 'downgrade'): c.refresh(self.output, client, NOW + timedelta(minutes=16))
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(client.json.call_count, 2)

    def test_incomplete_same_cycle_cache_is_rebuilt(self):
        self.write()
        product = json.loads(self.output.read_text())
        product['records'].pop()
        c.atomic_json(self.output, product)
        result = c.refresh(self.output, client_for(self.metadata, self.responses), NOW + timedelta(minutes=16))
        self.assertEqual(result['status'], 'written')
        self.assertEqual(len(json.loads(self.output.read_text())['records']), 4)

    def test_malformed_local_gate_can_recover(self):
        c.atomic_json(self.output.with_name('.point_cache_state.json'), {'next_check_at': 'broken'})
        self.assertEqual(self.write()['status'], 'written')

    def test_atomic_replace_failure_and_serialization_failure_preserve_file(self):
        self.output.write_text('{"old":true}\n')
        with patch('claef_point_forecast.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError): c.atomic_json(self.output, {'new': True})
        self.assertEqual(self.output.read_text(), '{"old":true}\n')
        self.assertEqual(list(self.output.parent.glob('*.tmp')), [])
        with self.assertRaises(ValueError): c.atomic_json(self.output, {'bad': float('nan')})
        self.assertEqual(self.output.read_text(), '{"old":true}\n')


class TransportTests(unittest.TestCase):
    @patch('claef_point_forecast.urlopen')
    def test_http_429_and_503_stop_without_retry(self, fetch):
        headers = Message(); headers['Retry-After'] = '1800'
        for code in (429, 503):
            fetch.reset_mock()
            fetch.side_effect = HTTPError('https://example.test', code, 'failure', headers, None)
            with self.assertRaisesRegex(c.RetrievalError, str(code)) as failure: c.ForecastClient().json('https://example.test')
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(failure.exception.retry_at is not None, code == 429)

    def test_retry_after_date_and_reset(self):
        headers = Message(); headers['Retry-After'] = 'Fri, 09 Oct 2026 08:00:00 GMT'; headers['ratelimit-reset'] = '60'
        self.assertEqual(c.retry_deadline(headers, NOW), NOW + timedelta(hours=2))

    @patch('claef_point_forecast.urlopen')
    def test_timeout_agent_invalid_response_and_network_errors(self, fetch):
        response = io.BytesIO(b'{"ok":true}'); fetch.return_value = response
        self.assertEqual(c.ForecastClient().json('https://example.test'), {'ok': True})
        self.assertEqual(fetch.call_args.kwargs['timeout'], 15)
        self.assertIn('PointCache', fetch.call_args.args[0].get_header('User-agent'))
        for body in (b'bad JSON', b'[]'):
            fetch.return_value = io.BytesIO(body)
            with self.assertRaises(c.RetrievalError): c.ForecastClient().json('https://example.test')
        for error in (URLError('DNS'), TimeoutError()):
            fetch.side_effect = error
            with self.assertRaises(c.RetrievalError): c.ForecastClient().json('https://example.test')

    @patch('claef_point_forecast.MAX_BYTES', 3)
    @patch('claef_point_forecast.urlopen')
    def test_payload_cap(self, fetch):
        fetch.return_value = io.BytesIO(b'1234')
        with self.assertRaisesRegex(c.RetrievalError, 'cap'): c.ForecastClient().json('https://example.test')

    @patch('claef_point_forecast.time.sleep')
    @patch('claef_point_forecast.time.monotonic', side_effect=[10, 10.2, 10.2])
    @patch('claef_point_forecast.urlopen')
    def test_request_spacing(self, fetch, monotonic, sleep):
        fetch.side_effect = [io.BytesIO(b'{}'), io.BytesIO(b'{}')]
        client = c.ForecastClient(); client.json('https://example.test'); client.json('https://example.test')
        self.assertAlmostEqual(sleep.call_args.args[0], .8)


if __name__ == '__main__':
    unittest.main()
