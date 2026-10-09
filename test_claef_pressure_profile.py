#!/usr/bin/env python3
"""Offline pressure-profile tests. Optional h5py enables tiny HDF5 integration."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import claef_pressure_profile as p


def source_metadata():
    metadata = {name: {'units': unit, 'standard_name': standard_name}
                for name, (standard_name, unit) in p.VARIABLES.items()}
    metadata['plev'] = {'units': 'hPa'}
    return metadata


def sample_values(count=3):
    return {'t': [20, 10, -10][:count], 'r': [50, 100, 0][:count],
            'u': [3, 0, None][:count], 'v': [4, -5, 2][:count],
            'z': [980.665, 1961.33, None][:count]}


class DerivedTests(unittest.TestCase):
    def test_pressure_order_and_native_values(self):
        levels = p.build_levels([700, 1000, 850], sample_values(), source_metadata())
        self.assertEqual([x['pressure_hpa'] for x in levels], [1000, 850, 700])
        self.assertEqual(levels[0]['temperature'], 10)
        self.assertEqual(levels[2]['relative_humidity'], 50)

    def test_missing_levels_are_not_interpolated(self):
        levels = p.build_levels([925, 500], sample_values(2), source_metadata())
        self.assertEqual([x['pressure_hpa'] for x in levels], [925, 500])
        self.assertNotIn(850, [x['pressure_hpa'] for x in levels])

    def test_units_packing_and_duplicate_levels_refused(self):
        for name, unit in [('t', 'K'), ('r', '1'), ('u', 'knots'), ('z', 'm'), ('plev', 'Pa')]:
            metadata = source_metadata(); metadata[name]['units'] = unit
            with self.subTest(name=name), self.assertRaises(p.ProbeError):
                p.build_levels([1000, 850, 700], sample_values(), metadata)
        metadata = source_metadata(); metadata['t']['scale_factor'] = .01
        with self.assertRaises(p.ProbeError): p.build_levels([1000, 850, 700], sample_values(), metadata)
        with self.assertRaises(p.ProbeError): p.build_levels([850, 850, 700], sample_values(), source_metadata())

    def test_magnus_dewpoint(self):
        self.assertAlmostEqual(p.dew_point_c(20, 50), 9.2611, places=3)
        self.assertAlmostEqual(p.dew_point_c(-10, 100), -10, places=7)
        for temperature, humidity in [(20, 0), (20, 101), (None, 80), (20, None), (-90, 80)]:
            self.assertIsNone(p.dew_point_c(temperature, humidity))

    def test_meteorological_wind_and_calm(self):
        self.assertAlmostEqual(p.wind_from_uv(3, 4)[0], 5)
        self.assertEqual(p.wind_from_uv(0, -5), (5, 0))
        self.assertEqual(p.wind_from_uv(-5, 0), (5, 90))
        self.assertEqual(p.wind_from_uv(0, 5), (5, 180))
        self.assertEqual(p.wind_from_uv(5, 0), (5, 270))
        self.assertEqual(p.wind_from_uv(0, 0), (0, None))
        self.assertEqual(p.wind_from_uv(None, 1), (None, None))

    def test_geopotential_height_and_missing_values(self):
        self.assertAlmostEqual(p.height_from_geopotential(980.665, 'm2 s-2', 'geopotential'), 100)
        self.assertIsNone(p.height_from_geopotential(None, 'm2 s-2', 'geopotential'))
        with self.assertRaises(p.ProbeError): p.height_from_geopotential(100, 'm', 'geopotential_height')
        levels = p.build_levels([1000, 850, 700], sample_values(), source_metadata(), 98000)
        self.assertTrue(levels[0]['below_model_surface'])
        self.assertFalse(levels[1]['below_model_surface'])
        self.assertIsNone(levels[2]['wind_speed'])
        self.assertIsNone(levels[2]['height_m'])
        self.assertIsNone(levels[2]['dew_point'])

    def test_fill_values_preserved_as_null(self):
        variable = Mock(); variable.attrs = {'_FillValue': -9999}
        self.assertEqual(p.native_values(variable, [1, float('nan'), -9999]), [1, None, None])

    def test_cf_time_origin_and_no_ambiguous_calendar(self):
        self.assertEqual(p.reference_from_time('hours since 2026-10-09 03:00:00', 'standard'), '2026-10-09T03:00:00Z')
        with self.assertRaises(p.ProbeError): p.reference_from_time('seconds since 2026-10-09', 'standard')
        with self.assertRaises(p.ProbeError): p.reference_from_time('hours since 2026-10-09', '360_day')


class RangeSafetyTests(unittest.TestCase):
    def selected(self):
        return {'key': 'resources/nwp-v2-1h-1km/filelisting/nwp_2026100903.nc', 'bytes': 9000000000, 'etag': 'abc'}

    def response(self, body, status=206, range_value='bytes 100-103/9000000000'):
        response = io.BytesIO(body); response.status = status
        response.headers = {'Content-Range': range_value, 'ETag': '"abc"', 'Content-Length': str(len(body))}
        return response

    @patch('claef_pressure_profile.urlopen')
    def test_range_and_transfer_accounting(self, fetch):
        fetch.return_value = self.response(b'abcd')
        client = p.TransferClient()
        self.assertEqual(client.get('https://example.test', 100, 103, 9000000000, 'abc'), b'abcd')
        self.assertEqual(client.bytes, 4); self.assertEqual(client.requests, 1)
        self.assertEqual(fetch.call_args.args[0].get_header('Range'), 'bytes=100-103')
        self.assertEqual(fetch.call_args.args[0].get_header('If-match'), '"abc"')
        self.assertEqual(fetch.call_args.kwargs['timeout'], 15)

    @patch('claef_pressure_profile.urlopen')
    def test_ignored_range_refused_before_reading(self, fetch):
        response = self.response(b'', status=200); response.read = Mock(side_effect=AssertionError('body must not be read'))
        fetch.return_value = response
        with self.assertRaises(p.SafetyRefusal): p.TransferClient().get('https://example.test', 100, 103, 9000000000, 'abc')
        response.read.assert_not_called()

    @patch('claef_pressure_profile.urlopen')
    def test_oversized_complete_file_budget_and_request_guards(self, fetch):
        client = p.TransferClient()
        with self.assertRaises(p.SafetyRefusal): client.get('https://example.test', 0, p.MAX_RANGE, 9000000000, 'abc')
        with self.assertRaises(p.SafetyRefusal): client.get('https://example.test', 0, 99, 100, 'abc')
        client.bytes = p.MAX_TOTAL
        with self.assertRaises(p.SafetyRefusal): client.get('https://example.test', 1, 4, 9000000000, 'abc')
        client.bytes = 0; client.requests = p.MAX_REQUESTS
        with self.assertRaises(p.SafetyRefusal): client.get('https://example.test', 1, 4, 9000000000, 'abc')
        fetch.assert_not_called()

    @patch('claef_pressure_profile.urlopen')
    def test_mismatched_etag_content_range_and_encoding(self, fetch):
        for header, value in [('ETag', '"changed"'), ('Content-Range', 'bytes 100-103/1000'), ('Content-Encoding', 'gzip'), ('Content-Length', '10000000000')]:
            response = self.response(b'abcd'); response.headers[header] = value
            fetch.return_value = response
            with self.subTest(header=header), self.assertRaises(p.SafetyRefusal):
                p.TransferClient().get('https://example.test', 100, 103, 9000000000, 'abc')

    @patch('claef_pressure_profile.urlopen')
    def test_429_has_no_retry(self, fetch):
        fetch.side_effect = HTTPError('https://example.test', 429, 'limited', {}, None)
        with self.assertRaisesRegex(p.ProbeError, '429'): p.TransferClient().get('https://example.test')
        self.assertEqual(fetch.call_count, 1)

    def test_cached_blocks_and_seek(self):
        client = Mock(); client.get.return_value = b'x' * p.METADATA_BLOCK
        reader = p.RangeFile(client, self.selected(), prefix=b'a' * p.METADATA_BLOCK)
        self.assertEqual(reader.read(3), b'aaa'); client.get.assert_not_called()
        reader.seek(p.METADATA_BLOCK + 10)
        self.assertEqual(reader.read(4), b'xxxx')
        reader.seek(p.METADATA_BLOCK + 20); self.assertEqual(reader.read(4), b'xxxx')
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(reader.seek(0, 2), 9000000000)
        with self.assertRaises(p.SafetyRefusal): reader.read(1)
        reader.seek(0)
        with self.assertRaises(p.SafetyRefusal): reader.read(p.MAX_RANGE + 1)


class SchemaTests(unittest.TestCase):
    def test_real_small_output_schema_and_accounting(self):
        # No HTTP: small saved extraction is an offline fixture.
        path = Path(__file__).resolve().parent / 'models/claef/profiles/latest/profile_f012.json'
        data = json.loads(path.read_text())
        self.assertEqual(data['dataset'], p.DATASET)
        self.assertEqual(data['lead_hours'], 12)
        self.assertEqual(data['pressure_levels_hpa'], [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300])
        self.assertEqual(len(data['levels']), 11)
        self.assertEqual(data['source_variables']['z']['units'], 'm2 s-2')
        self.assertEqual(data['source_variables']['t']['dimensions'], ['time', 'plev', 'latitude', 'longitude'])
        self.assertAlmostEqual(data['grid_point']['latitude'], 46.062)
        self.assertIn('dew_point', data['derived_fields'])
        self.assertLess(data['remote_access']['body_bytes_transferred'], p.MAX_TOTAL)
        self.assertLessEqual(data['remote_access']['http_requests'], p.MAX_REQUESTS)
        from probe_claef import parse_time
        self.assertEqual((parse_time(data['valid_time']) - parse_time(data['reference_time'])).total_seconds(), 43200)

    @patch('claef_pressure_profile.inspect_and_extract')
    def test_missing_explicit_cycle_is_never_substituted(self, extractor):
        client = Mock()
        discovery = ({'parameters': []}, [{'key': 'resources/nwp-v2-1h-1km/filelisting/nwp_2026100903.nc', 'bytes': 9000000000, 'etag': 'abc'}])
        with self.assertRaisesRegex(p.ProbeError, 'absent'):
            p.extract(client=client, discovery=discovery, reference_time='2026-10-09T00:00Z')
        client.get.assert_not_called(); extractor.assert_not_called()

    @patch('claef_pressure_profile.inspect_and_extract', side_effect=p.SafetyRefusal('oversized chunk'))
    def test_failure_preserves_previous_output(self, extract):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'profile.json'; output.write_text('{"previous":true}\n')
            client = p.TransferClient()
            discovery = ({'parameters': []}, [{'key': 'resources/nwp-v2-1h-1km/filelisting/nwp_2026100903.nc', 'bytes': 9000000000, 'etag': 'abc'}])
            with self.assertRaises(p.SafetyRefusal): p.extract(output, client=client, discovery=discovery, prefix=b'x')
            self.assertEqual(output.read_text(), '{"previous":true}\n')


@unittest.skipUnless(importlib.util.find_spec('h5py'), 'optional h5py is not installed')
class HDFIntegrationTests(unittest.TestCase):
    def test_tiny_offline_compressed_profile_extraction(self):
        import h5py
        import numpy as np
        buffer = io.BytesIO()
        with h5py.File(buffer, 'w') as nc:
            nc.attrs.update({'crs': 'EPSG:4326', 'date': '20261009', 'run': '03'})
            coordinates = {'time': ([0, 12], 'hours since 2026-10-09 03:00:00'),
                           'plev': ([925, 500], 'hPa'), 'latitude': ([46.0, 46.1], 'degrees_north'),
                           'longitude': ([14.5, 14.6], 'degrees_east')}
            for name, (values, units) in coordinates.items():
                variable = nc.create_dataset(name, data=np.array(values, dtype=float))
                variable.make_scale(name); variable.attrs['units'] = units
            for name, (standard_name, units) in p.VARIABLES.items():
                value = {'t': 20, 'r': 50, 'u': 3, 'v': 4, 'z': 980.665}[name]
                variable = nc.create_dataset(name, data=np.full((2, 2, 2, 2), value), chunks=(1, 2, 2, 2), compression='gzip')
                variable.attrs.update({'units': units, 'standard_name': standard_name})
                for i, coord in enumerate(['time', 'plev', 'latitude', 'longitude']): variable.dims[i].attach_scale(nc[coord])
            variable = nc.create_dataset('sp', data=np.full((2, 2, 2), 98000), chunks=(1, 2, 2))
            variable.attrs['units'] = 'Pa'
            for i, coord in enumerate(['time', 'latitude', 'longitude']): variable.dims[i].attach_scale(nc[coord])
        buffer.seek(0)
        buffer.selected = {'key': 'resources/nwp-v2-1h-1km/filelisting/nwp_2026100903.nc', 'etag': 'offline'}
        buffer.size = len(buffer.getvalue()); buffer.url = 'https://example.test/offline.nc'
        data = p.inspect_and_extract(buffer, 12)
        self.assertEqual(data['pressure_levels_hpa'], [925, 500])
        self.assertEqual(data['levels'][0]['wind_speed'], 5)
        self.assertAlmostEqual(data['levels'][0]['height_m'], 100)
        self.assertAlmostEqual(data['levels'][0]['dew_point'], 9.2611, places=3)
        self.assertEqual(data['grid_point']['latitude_index'], 1)


if __name__ == '__main__':
    unittest.main()
