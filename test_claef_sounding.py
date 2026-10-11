"""Offline production sounding, NetCDF validation and verification publication."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import claef_pressure_profile as p
import extract_claef as c
from icon_d2_forecast import publish_latest


def product(run='2026-10-09T00:00:00Z'):
    data = json.loads((Path(__file__).parent / 'models/claef/profiles/latest/profile_f012.json').read_text())
    data['reference_time'] = run
    data['valid_time'] = (c.parse_time(run) + c.timedelta(hours=12)).isoformat().replace('+00:00', 'Z')
    data['surface'] = {'sp': 98000, 'zsurf': 2941.995, '2t': 20, '2r': 50, '10u': 3, '10v': 4}
    for row in data['levels']:
        row['omega_pa_s'] = -0.25
    return data


class SoundingTests(unittest.TestCase):
    def test_exact_verification_rules(self):
        for stamp in ('2026-10-09T00:00Z', '2026-10-09T12:00Z'):
            c.validate_run(c.parse_time(stamp), 12, True)
        for stamp, lead in [('2026-10-09T03:00Z', 12), ('2026-10-09T00:01Z', 12),
                            ('2026-10-09T00:00:01Z', 12), ('2026-10-09T00:00Z', 3),
                            ('2026-10-09T00:00+02:00', 12)]:
            with self.subTest(stamp=stamp, lead=lead), self.assertRaises(ValueError):
                c.validate_run(c.parse_time(stamp), lead, True)
        with patch.object(c, 'extract_product') as fetch:
            with self.assertRaises(SystemExit): c.main(['--publish-verification'])
            fetch.assert_not_called()

    def test_surface_mapping_metadata_and_sparse_diagnostics(self):
        data = product()
        result = c.build_sounding(data)
        surface = result['levels'][0]
        self.assertEqual(surface['pressure_hpa'], 980)
        self.assertAlmostEqual(surface['height_m'], 300)
        self.assertEqual(surface['temperature_c'], 20)
        self.assertAlmostEqual(surface['dewpoint_c'], 9.2611, places=3)
        self.assertEqual(surface['relative_humidity_pct'], 50)
        self.assertEqual((surface['u_ms'], surface['v_ms'], surface['wind_speed_ms']), (3, 4, 5))
        self.assertAlmostEqual(surface['wind_direction_deg'], 216.8698976)
        self.assertEqual(surface['temperature_height_agl_m'], 2)
        self.assertEqual(surface['wind_height_agl_m'], 10)
        self.assertEqual(result['excluded_pressure_levels_hpa'], [1000])
        self.assertNotIn(1000, [row['pressure_hpa'] for row in result['levels']])
        for original, mapped in zip(data['levels'][1:], result['levels'][1:]):
            for source, target in [('temperature', 'temperature_c'), ('dew_point', 'dewpoint_c'),
                                   ('relative_humidity', 'relative_humidity_pct'), ('u', 'u_ms'),
                                   ('v', 'v_ms'), ('height_m', 'height_m'), ('omega_pa_s', 'omega_pa_s')]:
                self.assertEqual(original[source], mapped[target])
            self.assertFalse(mapped['below_model_surface'])
        self.assertEqual(result['run_time'], '2026-10-09T00:00:00Z')
        self.assertEqual(result['valid_time'], '2026-10-09T12:00:00Z')
        self.assertEqual(result['term'], '12')
        self.assertEqual(result['nominal_date'], '2026-10-09')
        self.assertEqual(result['parameters']['metpy'], {})
        self.assertFalse(result['parameters']['inversions']['available'])
        self.assertFalse(result['diagnostics_available']['parcel'])
        self.assertEqual(result['parameters']['standard_levels']['t850'], data['levels'][4]['temperature'])

    def test_missing_levels_never_interpolated(self):
        data = product()
        data['levels'] = [row for row in data['levels'] if row['pressure_hpa'] != 850]
        result = c.build_sounding(data)
        self.assertIsNone(result['parameters']['standard_levels']['t850'])
        self.assertNotIn(850, [row['pressure_hpa'] for row in result['levels']])

    def test_explicit_missing_cycle_never_falls_back(self):
        client = Mock()
        discovery = ({'parameters': []}, [{'key': 'resources/nwp-v2-1h-1km/filelisting/nwp_2026100903.nc', 'bytes': 9000000000, 'etag': 'abc'}])
        with self.assertRaisesRegex(p.ProbeError, 'absent'):
            p.extract_product(client=client, discovery=discovery, reference_time='2026-10-09T00:00Z', sounding=True)
        client.get.assert_not_called()

    def test_valid_time_mismatch_refused(self):
        data = product(); data['valid_time'] = data['reference_time']
        with self.assertRaises(p.ProbeError): c.build_sounding(data)

    def test_generation_uses_in_memory_exact_run_and_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'models' / 'claef'
            with patch.object(c, 'extract_product', return_value=product()) as fetch, patch('render_ljlm.render_products', return_value={}) as render:
                output = c.generate(c.parse_time('2026-10-09T00:00Z'), root=root)
            self.assertEqual(output, root / '2026100900/ljlm_f012.json')
            fetch.assert_called_once_with(lead_hours=12, reference_time='2026-10-09T00:00:00Z', sounding=True)
            self.assertEqual(render.call_args.kwargs['diagnostics_root'], root / '2026100900/diagnostics/f012')
            self.assertEqual(json.loads(output.read_text())['model'], 'C-LAEF')

    def test_failed_or_substituted_extraction_preserves_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'models' / 'claef'
            output = root / '2026100900/ljlm_f012.json'
            c.atomic_json(output, {'previous': True})
            for extracted in (p.SafetyRefusal('transfer budget'), product('2026-10-09T03:00:00Z')):
                options = {'side_effect': extracted} if isinstance(extracted, Exception) else {'return_value': extracted}
                with patch.object(c, 'extract_product', **options), patch('render_ljlm.render_products') as render:
                    with self.assertRaises(p.ProbeError):
                        c.generate(c.parse_time('2026-10-09T00:00Z'), publish_verification=True, root=root)
                    render.assert_not_called()
                self.assertEqual(json.loads(output.read_text()), {'previous': True})
                self.assertFalse((root / 'latest').exists())

    def test_latest_slots_and_no_regression(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'models' / 'claef'
            for stamp, slot in [('2026-10-09T00:00:00Z', '12'), ('2026-10-09T12:00:00Z', '00'), ('2026-10-08T00:00:00Z', '12')]:
                profile = c.build_sounding(product(stamp))
                directory = root / c.parse_time(stamp).strftime('%Y%m%d%H')
                output = directory / 'ljlm_f012.json'
                c.atomic_json(output, profile)
                image = directory / 'skewt.png'; image.write_bytes(b'offline image')
                manifest = {key: profile[key] for key in ('model', 'run_time', 'valid_time', 'lead_hours', 'sounding_id')}
                manifest.update(latest={'skewt': str(image)}, archive={'skewt': str(image)})
                destination = publish_latest(output, manifest, root)
                self.assertEqual(destination, root / 'latest' / slot)
                saved = json.loads((destination / 'sounding.json').read_text())
                self.assertEqual(saved['run_time'], '2026-10-09T00:00:00Z' if slot == '12' else stamp)
                self.assertEqual((destination / 'skewt.png').read_bytes(), b'offline image')
                published = json.loads((destination / 'latest_products.json').read_text())
                self.assertEqual(published['verification_term'], slot)


@unittest.skipUnless(importlib.util.find_spec('h5py'), 'optional h5py is not installed')
class NetCDFTests(unittest.TestCase):
    def fixture(self):
        import h5py
        import numpy as np
        buffer = io.BytesIO()
        with h5py.File(buffer, 'w') as nc:
            nc.attrs.update(crs='EPSG:4326', date='20261009', run='00')
            coords = {'time': ([0, 12], 'hours since 2026-10-09 00:00:00'),
                      'plev': (p.EXPECTED_PRESSURES, 'hPa'),
                      'latitude': ([46.0, 46.1], 'degrees_north'), 'longitude': ([14.5, 14.6], 'degrees_east')}
            for name, (values, unit) in coords.items():
                var = nc.create_dataset(name, data=np.array(values, dtype=float))
                var.make_scale(name); var.attrs['units'] = unit
            values = {'t': 10, 'r': 50, 'u': 3, 'v': 4, 'z': 49033.25, 'w': -.2,
                      'sp': 98000, 'zsurf': 2941.995, '2t': 20, '2r': 50, '10u': 3, '10v': 4}
            for name, (standard, unit) in {**p.SOUNDING_VARIABLES, **p.SURFACE_VARIABLES}.items():
                dims = ['time', 'plev', 'latitude', 'longitude'] if name in p.SOUNDING_VARIABLES else ['time', 'latitude', 'longitude']
                shape = tuple(len(coords[d][0]) for d in dims)
                var = nc.create_dataset(name, data=np.full(shape, values[name]), chunks=(1, *shape[1:]), compression='gzip')
                var.attrs.update(units=unit, standard_name=standard)
                for i, dim in enumerate(dims): var.dims[i].attach_scale(nc[dim])
        buffer.seek(0)
        buffer.selected = {'key': 'resources/nwp-v2-1h-1km/filelisting/nwp_2026100900.nc', 'etag': 'offline'}
        buffer.size = len(buffer.getvalue()); buffer.url = 'https://example.test/offline.nc'
        return buffer

    def test_full_selected_time_extraction(self):
        import h5py
        buffer = self.fixture()
        with h5py.File(buffer, 'r+') as nc:
            nc['2t'][0, :, :] = -10
            nc['w'][0, :, :, :] = 1
        data = p.inspect_and_extract(buffer, 12, sounding=True)
        self.assertEqual(data['surface']['2t'], 20)
        self.assertEqual(data['levels'][0]['omega_pa_s'], -.2)
        self.assertEqual(data['reference_time'], '2026-10-09T00:00:00Z')
        self.assertEqual(data['valid_time'], '2026-10-09T12:00:00Z')
        self.assertEqual(data['pressure_levels_hpa'], p.EXPECTED_PRESSURES)

    def test_units_dimensions_packing_and_times_refused(self):
        import h5py
        mutations = [('w', 'units', 'm s-1'), ('sp', 'scale_factor', .01),
                     ('zsurf', 'standard_name', 'geopotential_height'), ('2t', 'units', 'K'),
                     ('2r', 'add_offset', 10), ('10u', 'scale_factor', .1), ('10v', 'units', 'knots'),
                     ('time', 'units', 'hours since 2026-10-09 03:00:00'), ('plev', 'scale_factor', 100)]
        for name, attr, value in mutations:
            buffer = self.fixture()
            with h5py.File(buffer, 'r+') as nc: nc[name].attrs[attr] = value
            with self.subTest(name=name, attr=attr), self.assertRaises(p.ProbeError):
                p.inspect_and_extract(buffer, 12, sounding=True)
        for mutation in ('dimensions', 'lead', 'levels'):
            buffer = self.fixture()
            with h5py.File(buffer, 'r+') as nc:
                if mutation == 'dimensions': nc['10v'].dims[0].detach_scale(nc['time'])
                if mutation == 'lead': nc['time'][1] = 13
                if mutation == 'levels': nc['plev'][-1] = 200
            with self.subTest(mutation=mutation), self.assertRaises(p.ProbeError):
                p.inspect_and_extract(buffer, 12, sounding=True)


if __name__ == '__main__':
    unittest.main()
