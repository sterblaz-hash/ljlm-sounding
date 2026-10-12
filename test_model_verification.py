from datetime import datetime, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import render_model_verification as renderer


def inputs(term='00'):
    obs = dict(station=14015, nominal_date='2026-10-12', term=term, sounding_id='obs')
    model = dict(station=14015, valid_time=f'2026-10-12T{term}:00:00Z', lead_hours=12,
                 run_time='2026-10-11T12:00:00Z' if term == '00' else '2026-10-12T00:00:00Z')
    def row(p, offset=0):
        return dict(pressure_hpa=p, temperature_c=math.log(p)*10+offset,
                    dewpoint_c=math.log(p)*10-5+offset, relative_humidity_pct=70+offset,
                    height_m=10000-math.log(p)*1000+offset, u_ms=2+offset, v_ms=3+offset)
    obs['levels'] = [row(p) for p in (1000, 900, 800, 600, 400, 200)]
    icon = {**model, 'sounding_id': 'icon', 'levels': [row(p, 2) for p in (1000, 900, 800, 600, 400, 200)]}
    claef = {**model, 'sounding_id': 'claef', 'levels': [dict(row(980, -3), level_type='model_surface')] +
             [dict(row(p, -3), level_type='pressure') for p in renderer.STANDARD_LEVELS]}
    return obs, icon, claef


class VerificationTests(unittest.TestCase):
    def test_exact_match_and_rejections(self):
        for term in ('00', '12'):
            obs, icon, claef = inputs(term)
            self.assertEqual(renderer.matching_time(obs, icon, claef), datetime(2026, 10, 12, int(term), tzinfo=timezone.utc))
            for index in (1, 2):
                for key, value in [('lead_hours', 6), ('valid_time', '2026-10-13T00:00:00Z'),
                                   ('run_time', '2026-10-11T00:00:00Z'), ('run_time', '2026-10-11T12:00:00'),
                                   ('station', 12345)]:
                    with self.subTest(term=term, index=index, key=key):
                        data = [obs, icon, claef]
                        data[index] = {**data[index], key: value}
                        self.assertIsNone(renderer.matching_time(*data))
            self.assertIsNone(renderer.matching_time({**obs, 'term': '06'}, icon, claef))

    def test_standard_interpolation_bias_and_surface(self):
        obs, icon, claef = inputs()
        result = renderer.comparison(obs, icon, claef, renderer.matching_time(obs, icon, claef))
        self.assertEqual([r['pressure_hpa'] for r in result['standard_pressure_comparisons']], list(renderer.STANDARD_LEVELS))
        for record in result['standard_pressure_comparisons']:
            p = record['pressure_hpa']
            self.assertAlmostEqual(record['values']['OBS']['temperature_c'], 10*math.log(p))
            self.assertAlmostEqual(record['values']['ICON-D2']['height_m'], 10002-1000*math.log(p))
            self.assertEqual(record['source_methods']['C-LAEF'], 'native_exact_pressure')
            for name, offset in [('ICON-D2', 2), ('C-LAEF', -3)]:
                bias = record['model_minus_obs'][name]
                for field in renderer.FIELDS:
                    self.assertAlmostEqual(bias[field], offset)
                self.assertAlmostEqual(bias['wind_vector_error_ms'], math.sqrt(2)*abs(offset))
        for name, data in zip(renderer.NAMES, (obs, icon, claef)):
            self.assertEqual(result['native_surfaces'][name]['row'], data['levels'][0])
        self.assertNotIn('surface_bias', result)

    def test_nulls_no_invented_claef_and_no_bridging(self):
        obs, icon, claef = inputs()
        claef['levels'] = [r for r in claef['levels'] if r['pressure_hpa'] != 700]
        claef['levels'][1]['dewpoint_c'] = None
        record = renderer.comparison(obs, icon, claef, renderer.matching_time(obs, icon, claef))['standard_pressure_comparisons']
        self.assertTrue(all(v is None for v in record[2]['values']['C-LAEF'].values()))
        self.assertTrue(all(v is None for v in record[2]['model_minus_obs']['C-LAEF'].values()))
        self.assertIsNone(record[0]['values']['C-LAEF']['dewpoint_c'])
        obs['levels'][1]['relative_humidity_pct'] = None
        self.assertIsNone(renderer.sample(obs, 925)[0]['relative_humidity_pct'])
        self.assertIsNone(renderer.sample(obs, 1100)[0]['temperature_c'])

    def test_native_wind_conversion(self):
        row = dict(pressure_hpa=850, wind_speed_ms=10, wind_direction_deg=270)
        values = renderer.row_values(row)
        self.assertAlmostEqual(values['u_ms'], 10)
        self.assertAlmostEqual(values['v_ms'], 0)
        self.assertIsNone(values['relative_humidity_pct'])

    def test_unexpected_symlink_rejected_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / 'latest' / '00'
            latest.mkdir(parents=True)
            marker = latest / 'comparison.json'
            marker.write_text('previous')
            original = Path.is_symlink
            with patch.object(Path, 'is_symlink', lambda p: p == latest or original(p)):
                with self.assertRaisesRegex(ValueError, 'normal directory'):
                    renderer.publish(root / 'unused', root, datetime(2026, 10, 12, tzinfo=timezone.utc), '00')
            self.assertEqual(marker.read_text(), 'previous')
            self.assertFalse((root / '2026').exists())

    def test_publication_png_and_failure_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / f'{name}.json' for name in renderer.NAMES]
            output = root / 'output'
            for term in ('00', '12'):
                data = inputs(term)
                for p, d in zip(paths, data):
                    p.write_text(json.dumps(d))
                (output / 'latest' / term).mkdir(parents=True)
                (output / 'latest' / term / 'comparison.json').write_text('previous')
                result = renderer.render_verification(*paths, output)
                self.assertIsNotNone(result)
                slot = output / 'latest' / term
                self.assertTrue(slot.is_dir())
                self.assertFalse(slot.is_symlink())
                for filename in ('comparison.json', 'skewt_overlay.png'):
                    self.assertTrue((slot / filename).is_file())
                    self.assertFalse((slot / filename).is_symlink())
                self.assertEqual(json.loads((slot / 'comparison.json').read_text()), result)
                with Image.open(slot / 'skewt_overlay.png') as image:
                    self.assertEqual(image.format, 'PNG')
                    self.assertGreater(image.width, 1000)
                    image.verify()
                archive = output / f'2026/10/20261012_{term}_comparison.json'
                self.assertEqual(archive.read_bytes(), (slot / 'comparison.json').read_bytes())
                self.assertEqual((archive.parent / f'20261012_{term}_skewt_overlay.png').read_bytes(), (slot / 'skewt_overlay.png').read_bytes())
                previous = {p: p.read_bytes() for p in [slot / 'comparison.json', slot / 'skewt_overlay.png', archive, archive.parent / f'20261012_{term}_skewt_overlay.png']}
                for action in ('missing_icon', 'missing_claef', 'mismatch', 'bad_json', 'render_error', 'invalid_png', 'publication_error', 'archive_error'):
                    with self.subTest(term=term, action=action):
                        for p, d in zip(paths, data):
                            p.write_text(json.dumps(d))
                        if action.startswith('missing'):
                            paths[1 if action == 'missing_icon' else 2].unlink()
                        if action == 'mismatch':
                            paths[2].write_text(json.dumps({**data[2], 'lead_hours': 6}))
                        if action == 'bad_json':
                            paths[0].write_text('{')
                        if action == 'render_error':
                            with patch.object(renderer, 'render_overlay', side_effect=RuntimeError('render failed')):
                                self.assertIsNone(renderer.render_verification(*paths, output))
                        elif action == 'invalid_png':
                            with patch.object(renderer, 'render_overlay', side_effect=lambda *args: args[-1].write_bytes(b'invalid')):
                                self.assertIsNone(renderer.render_verification(*paths, output))
                        elif action in ('publication_error', 'archive_error'):
                            original = Path.replace
                            def fail_publication(path, target):
                                if ((action == 'publication_error' and path.name == 'completed')
                                        or (action == 'archive_error' and path.name == 'archive_skewt_overlay.png')):
                                    raise OSError('publication failed')
                                return original(path, target)
                            with patch.object(Path, 'replace', fail_publication):
                                self.assertIsNone(renderer.render_verification(*paths, output))
                        else:
                            self.assertIsNone(renderer.render_verification(*paths, output))
                        self.assertEqual(previous, {p: p.read_bytes() for p in previous})


if __name__ == '__main__':
    unittest.main()
