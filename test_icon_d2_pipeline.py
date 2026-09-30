"""Offline checks for native heights, profile validation and model labeling."""
import unittest
from unittest.mock import patch

from extract_icon_d2 import validate_levels
from icon_d2_native import add_native_heights, fetch_json
from render_ljlm import _nominal_title


class ModelSoundingTests(unittest.TestCase):
    def test_shared_json_fetch(self):
        with patch('icon_d2_native.fetch_bytes', return_value=b'{"ok": true}'):
            self.assertEqual(fetch_json('https://example.test'), {'ok': True})

    def test_native_height_midpoint_and_terrain(self):
        profile = dict(run_time='2026-09-30T09:00:00Z', requested_latitude=46.06,
                       requested_longitude=14.51, grid_latitude=46.06,
                       grid_longitude=14.51, levels=[{'model_level': 65}])
        listing = ''.join(f'href="icon-d2_germany_regular-lat-lon_time-invariant_2026093009_000_{k}_hhl.grib2.bz2"' for k in (65, 66)).encode()
        def fetch(url, **kwargs):
            return listing if url.endswith('/hhl/') else (b'65' if '_65_' in url else b'66')
        def decode(payload, index):
            k = int(payload)
            return (350 if k == 65 else 300), {'units': 'm', 'level': k}
        with patch('icon_d2_native.fetch_bytes', side_effect=fetch), patch(
            'icon_d2_native.nearest_grid_index_from_payload', return_value=(0, 46.06, 14.51)
        ), patch('icon_d2_native.value_from_payload', side_effect=decode):
            add_native_heights(profile, 1)
        self.assertEqual(profile['levels'][0]['height_m'], 325)
        self.assertEqual(profile['grid_surface_height_m'], 300)

    def test_reject_inverted_height_profile(self):
        rows = [dict(model_level=65-i, pressure_hpa=1000-i*10, height_m=i*100,
                     temperature_c=10, dewpoint_c=5, wind_speed_ms=5,
                     wind_direction_deg=180) for i in range(10)]
        validate_levels(rows)
        rows[5]['height_m'] = 0
        with self.assertRaisesRegex(ValueError, 'ordering'):
            validate_levels(rows)

    def test_titles_distinguish_forecast_without_changing_observed(self):
        self.assertEqual(_nominal_title(dict(nominal_date='2026-09-30', term='00')),
                         '30 Sep 2026 · 00 UTC')
        title = _nominal_title(dict(profile_type='model', model='ICON-D2',
                                   run_time='2026-09-30T09:00:00Z',
                                   valid_time='2026-09-30T12:00:00Z', lead_hours=3))
        self.assertIn('+003h', title)
        self.assertIn('valid 30 Sep 12 UTC', title)


if __name__ == '__main__':
    unittest.main()
