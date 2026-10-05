from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import render_icon_d2_verification as renderer


def profiles(term='00'):
    obs = {'nominal_date': '2026-10-05', 'term': term, 'sounding_id': 'obs-' + term,
           'launch_time': '2026-10-04T23:30:00Z', 'processed_at': '2099-01-01T00:00:00Z'}
    model = {'valid_time': f'2026-10-05T{term}:00:00Z', 'lead_hours': 12,
             'run_time': '2026-10-04T12:00:00Z' if term == '00' else '2026-10-05T00:00:00Z',
             'sounding_id': 'model-' + term}
    levels = [dict(pressure_hpa=p, height_m=z, temperature_c=t, dewpoint_c=t-5,
                   wind_speed_ms=10, wind_direction_deg=270)
              for p, z, t in [(p, (1000-p)*17, 20-(1000-p)*.09) for p in range(1000, 99, -25)]]
    obs['levels'] = levels
    model['levels'] = levels
    return obs, model


class VerificationTests(unittest.TestCase):
    def test_match_and_nominal_time(self):
        obs, model = profiles()
        self.assertEqual(renderer.matching_time(obs, model), datetime(2026, 10, 5, tzinfo=timezone.utc))
        for key, value in [('lead_hours', 3), ('valid_time', '2026-10-06T00:00:00Z'),
                           ('run_time', '2026-10-04T00:00:00Z')]:
            with self.subTest(key=key):
                self.assertIsNone(renderer.matching_time(obs, {**model, key: value}))
        self.assertIsNone(renderer.matching_time({**obs, 'term': 'special'}, model))

    def test_render_slots_manifest_and_safe_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for term in ('00', '12'):
                obs, model = profiles(term)
                op, mp = root / 'obs.json', root / 'model.json'
                op.write_text(json.dumps(obs)); mp.write_text(json.dumps(model))
                output = root / 'verification'
                manifest = renderer.render_verification(op, mp, output)
                self.assertEqual(manifest['observation_sounding_id'], obs['sounding_id'])
                self.assertEqual(manifest['model_sounding_id'], model['sounding_id'])
                self.assertEqual(manifest['valid_time'], model['valid_time'])
                png = output / 'latest' / term / 'skewt_overlay.png'
                self.assertEqual(png.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
                json.dumps(manifest, allow_nan=False)
                before = {p: p.read_bytes() for p in output.rglob('*') if p.is_file()}
                mp.write_text(json.dumps({**model, 'lead_hours': 6}))
                self.assertIsNone(renderer.render_verification(op, mp, output))
                mp.unlink()
                self.assertIsNone(renderer.render_verification(op, mp, output))
                self.assertEqual(before, {p: p.read_bytes() for p in before})
            self.assertTrue((output / 'latest/00/latest_products.json').is_file())
            self.assertTrue((output / 'latest/12/latest_products.json').is_file())

    def test_pressure_and_wind_coverage(self):
        obs, model = profiles()
        a = renderer.style._clean_profile(obs['levels'])
        b = renderer.style._clean_profile([row for row in model['levels'] if 500 <= row['pressure_hpa'] <= 850])
        self.assertEqual(renderer.common_pressure_bounds(a, b), (850, 500))
        u, v = renderer.barb_values(b, [1000, 700, 100])
        self.assertTrue(np.isnan(u[0]) and np.isnan(u[2]))
        self.assertTrue(np.isfinite(u[1]) and np.isfinite(v[1]))


if __name__ == '__main__':
    unittest.main()
