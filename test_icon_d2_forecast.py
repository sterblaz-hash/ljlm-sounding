"""Offline checks for fixed-cycle +12 h forecasts and latest-slot publication."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from extract_icon_d2 import build_profile, main
from icon_d2_forecast import choose_run, publish_latest, verification_time
from icon_d2_native import utc_iso

UTC = timezone.utc


class ForecastTimingTests(unittest.TestCase):
    def check_profile_time(self, run, expected):
        def native(lat, lon, actual_run, valid, top, workers):
            self.assertEqual(actual_run, run)
            self.assertEqual(valid, expected)
            return dict(run_time=utc_iso(actual_run), valid_time=utc_iso(valid),
                        lead_hours=int((valid-actual_run).total_seconds()/3600), levels=[])
        with patch('extract_icon_d2.fetch_dwd_native_profile', side_effect=native), \
             patch('extract_icon_d2.add_native_heights'), \
             patch('extract_icon_d2.validate_levels'), \
             patch('extract_icon_d2.calculate_metpy_parameters', return_value={}), \
             patch('extract_icon_d2.calculate_inversions', return_value={}), \
             patch('extract_icon_d2.value_at_pressure', return_value=None):
            result = build_profile(run, 12, 1)
        self.assertEqual(result['lead_hours'], 12)
        self.assertEqual(result['valid_time'], utc_iso(expected))
        self.assertEqual(result['nominal_date'], expected.strftime('%Y-%m-%d'))
        self.assertEqual(result['term'], expected.strftime('%H'))
        self.assertEqual(verification_time(run, 12), expected)

    def test_00_run_plus_12_verifies_at_12(self):
        self.check_profile_time(datetime(2026, 9, 30, 0, tzinfo=UTC),
                                datetime(2026, 9, 30, 12, tzinfo=UTC))

    def test_12_run_plus_12_verifies_at_next_day_00(self):
        self.check_profile_time(datetime(2026, 9, 30, 12, tzinfo=UTC),
                                datetime(2026, 10, 1, 0, tzinfo=UTC))

    def test_only_00_12_cycles_and_12_hour_lead(self):
        for hour in (3, 6, 9, 15, 18, 21):
            with self.subTest(hour=hour), self.assertRaises(ValueError):
                verification_time(datetime(2026, 9, 30, hour, tzinfo=UTC), 12)
        for lead in (0, 3, 6, 24):
            with self.subTest(lead=lead), self.assertRaises(ValueError):
                verification_time(datetime(2026, 9, 30, 0, tzinfo=UTC), lead)

    def test_delayed_schedule_keeps_explicit_cycle(self):
        self.assertEqual(choose_run('00', now=datetime(2026, 9, 30, 16, tzinfo=UTC), scheduled=True),
                         datetime(2026, 9, 30, 0, tzinfo=UTC))
        self.assertEqual(choose_run('12', now=datetime(2026, 10, 1, 0, tzinfo=UTC), scheduled=True),
                         datetime(2026, 9, 30, 12, tzinfo=UTC))

    def test_cli_rejects_implicit_or_wrong_cycle_before_download(self):
        for args in (['--lead-hours', '12'],
                     ['--run', '2026-09-30T03:00Z', '--lead-hours', '12'],
                     ['--run', '2026-09-30T00:00Z', '--lead-hours', '0']):
            with self.subTest(args=args), patch('sys.argv', ['extract_icon_d2.py', '--publish-verification'] + args), \
                 patch('extract_icon_d2.select_run') as select, \
                 patch('extract_icon_d2.build_profile') as build:
                with self.assertRaises(SystemExit) as exc:
                    main()
                self.assertEqual(exc.exception.code, 2)
                select.assert_not_called()
                build.assert_not_called()

    def test_unavailable_explicit_run_fails_without_fallback(self):
        with patch('sys.argv', ['extract_icon_d2.py', '--publish-verification', '--run',
                               '2026-09-30T00:00Z', '--lead-hours', '12']), \
             patch('extract_icon_d2.select_run') as select, \
             patch('extract_icon_d2.build_profile', side_effect=RuntimeError('unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                main()
            select.assert_not_called()


class LatestForecastTests(unittest.TestCase):
    def fixture(self, root, run):
        valid = verification_time(run, 12)
        directory = root / run.strftime('%Y%m%d%H')
        directory.mkdir(parents=True, exist_ok=True)
        profile = dict(model='ICON-D2', run_time=utc_iso(run), valid_time=utc_iso(valid),
                       lead_hours=12, sounding_id=run.strftime('icon-d2_%Y%m%d%H_f012'))
        output = directory / 'ljlm_f012.json'
        output.write_text(json.dumps(profile))
        png = directory / 'skewt.png'
        png.write_bytes(b'test image')
        manifest = dict(profile, latest={'skewt': str(png)}, archive={'skewt': str(png)})
        return output, manifest

    def test_both_slots_preserve_metadata_and_do_not_regress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'models' / 'icon-d2'
            for hour, slot in ((0, '12'), (12, '00')):
                output, manifest = self.fixture(root, datetime(2026, 9, 30, hour, tzinfo=UTC))
                publish_latest(output, manifest, root)
                dest = root / 'latest' / slot
                result = json.loads((dest / 'latest_products.json').read_text())
                for key in ('run_time', 'valid_time', 'lead_hours'):
                    self.assertEqual(result[key], manifest[key])
                self.assertEqual(result['verification_term'], slot)
                self.assertTrue((Path(tmp) / result['latest']['skewt']).is_file())
                self.assertTrue((Path(tmp) / result['archive']['skewt']).is_file())
                self.assertFalse(Path(result['archive']['skewt']).is_absolute())
                original = (dest / 'sounding.json').read_bytes()
                old, old_manifest = self.fixture(root, datetime(2026, 9, 29, hour, tzinfo=UTC))
                publish_latest(old, old_manifest, root)
                self.assertEqual((dest / 'sounding.json').read_bytes(), original)

    def test_missing_graphics_preserve_existing_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'models' / 'icon-d2'
            output, manifest = self.fixture(root, datetime(2026, 9, 29, 0, tzinfo=UTC))
            destination = publish_latest(output, manifest, root)
            before = {p.name: p.read_bytes() for p in destination.iterdir()}
            output, manifest = self.fixture(root, datetime(2026, 9, 30, 0, tzinfo=UTC))
            Path(manifest['latest']['skewt']).unlink()
            with self.assertRaises(FileNotFoundError):
                publish_latest(output, manifest, root)
            self.assertEqual({p.name: p.read_bytes() for p in destination.iterdir()}, before)

    def test_mismatched_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'models' / 'icon-d2'
            output, manifest = self.fixture(root, datetime(2026, 9, 30, 0, tzinfo=UTC))
            manifest['lead_hours'] = 0
            with self.assertRaisesRegex(ValueError, 'manifest disagrees'):
                publish_latest(output, manifest, root)
            self.assertFalse((root / 'latest').exists())


if __name__ == '__main__':
    unittest.main()
