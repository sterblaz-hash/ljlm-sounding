from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_dashboard_data as builder


def sounding(date='2026-10-02', term='00'):
    return {'sounding_id': date.replace('-', '') + '_' + term,
            'nominal_date': date, 'term': term, 'launch_time': None,
            'processed_at': '2026-10-02T02:00:00Z'}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def put(self, path, value):
        target = self.root / path
        builder.write_json(target, value)
        return target

    def test_extract_every_metric(self):
        source = sounding()
        for index, (key, path) in enumerate(builder.PATHS.items()):
            target = source
            for part in path[:-1]:
                target = target.setdefault(part, {})
            target[path[-1]] = index + 0.25
        record = builder.extract_record(source)
        for index, key in enumerate(builder.VARIABLES):
            self.assertEqual(record[key], index + 0.25)
        self.assertEqual(record['term'], '00')
        self.assertNotIn('levels', record)

    def test_valid_time_uses_nominal_date_and_term(self):
        for term, launch in (("00", "2026-10-01T23:30:00Z"),
                             ("12", "2026-10-02T11:30:00Z")):
            with self.subTest(term=term):
                source = sounding(term=term)
                source["launch_time"] = launch
                source["valid_time"] = launch
                record = builder.extract_record(source)
                self.assertEqual(record["valid_time"], f"2026-10-02T{term}:00:00Z")
                self.assertEqual(record["launch_time"], launch)
        self.assertEqual(builder.extract_record(sounding())["valid_time"],
                         "2026-10-02T00:00:00Z")

    def test_fixed_as_of_backfill_is_byte_identical(self):
        self.put("data/2026/10/20261002_00.json", sounding())
        self.put("data/latest.json", sounding())
        now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        def rebuild():
            builder.backfill(self.root)
            builder.build_windows(self.root, now)
            return {p: p.read_bytes() for p in (self.root / builder.BASE).rglob("*.json")}
        first = rebuild()
        self.assertEqual(first, rebuild())
        for path in first:
            for record in builder.read_json(path)["records"]:
                self.assertEqual(record["valid_time"], "2026-10-02T00:00:00Z")

    def test_legacy_id(self):
        source = sounding()
        del source["sounding_id"]
        self.assertEqual(builder.extract_record(source)["sounding_id"], "20261002_00")

    def test_real_schema(self):
        # Check representative mappings independently against the existing OBS schema.
        source = sounding()
        source['parameters'] = {
            'standard_levels': {'t850': {'value': 8.07}},
            'metpy': {'pwat_mm': 12.37, 'sbcape_jkg': 0,
                      'moisture_transport': {
                          'humidity_profile': {'850': {'specific_humidity_gkg': 4.09}},
                          'ivt': {'magnitude_kg_m1_s1': 44.4, 'transport_to_direction_deg': 269.4},
                          'orographic_cross_barrier': {'barriers': {'dinaric_west': {
                              'terrain_capped_ivt': {'signed_cross_barrier_kg_m1_s1': -19.7,
                                                     'signed_fraction': -0.789}}}}}}}
        record = builder.extract_record(source)
        for key, value in {'t850_c': 8.07, 'pwat_mm': 12.37, 'q850_gkg': 4.09,
                           'ivt_kg_m1_s1': 44.4, 'ivt_direction_deg': 269.4,
                           'dinaric_west_signed_ivt': -19.7,
                           'dinaric_west_signed_fraction': -0.789, 'sbcape_jkg': 0}.items():
            self.assertEqual(record[key], value)

    def test_missing_nonfinite_and_unavailable_are_null(self):
        source = sounding()
        self.assertTrue(all(builder.extract_record(source)[k] is None for k in builder.VARIABLES))
        source['parameters'] = {'metpy': {'pwat_mm': float('nan'), 'sbcape_jkg': float('inf'),
                                         'moisture_transport': {'available': False,
                                                               'ivt': {'magnitude_kg_m1_s1': 9}}}}
        record = builder.extract_record(source)
        self.assertIsNone(record['pwat_mm'])
        self.assertIsNone(record['sbcape_jkg'])
        self.assertIsNone(record['ivt_kg_m1_s1'])
        json.dumps(record, allow_nan=False)
        source['parameters'] = None
        self.assertIsNone(builder.extract_record(source)['t850_c'])

    def test_update_replace_sort_and_terms(self):
        path = self.put('data/latest.json', sounding(term='12'))
        builder.update(self.root, path)
        self.put('data/latest.json', sounding())
        builder.update(self.root, path)
        changed = sounding()
        changed['parameters'] = {'metpy': {'pwat_mm': 5}}
        self.put('data/latest.json', changed)
        builder.update(self.root, path)
        month = self.root / 'timeseries/ljlm/archive/2026/10.json'
        before = month.read_bytes()
        builder.update(self.root, path)
        self.assertEqual(before, month.read_bytes())
        records = builder.read_json(month)['records']
        self.assertEqual([r['term'] for r in records], ['00', '12'])
        self.assertEqual(records[0]['pwat_mm'], 5)

    def test_window_boundaries(self):
        end = datetime(2026, 10, 2, tzinfo=timezone.utc)
        for days in (7, 30, 90, 365):
            with self.subTest(days=days):
                dates = [end + timedelta(days=1), end, end - timedelta(days=days),
                         end - timedelta(days=days + 1)]
                records = [builder.extract_record(sounding(d.date().isoformat())) for d in dates]
                selected = builder.filter_window(records, end, days)
                self.assertEqual([r['nominal_date'] for r in selected],
                                 [dates[2].date().isoformat(), dates[1].date().isoformat()])

    def test_windows_read_only_relevant_compact_months(self):
        source = self.put('data/latest.json', sounding())
        builder.update(self.root, source)
        self.put('timeseries/ljlm/archive/1900/01.json', {'invalid': True})
        real_read = builder.read_json
        accessed = []
        def read(path):
            accessed.append(path.relative_to(self.root).as_posix())
            self.assertIn('timeseries/ljlm/archive/', accessed[-1])
            return real_read(path)
        with patch.object(builder, 'read_json', side_effect=read):
            builder.build_windows(self.root, datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(accessed, ['timeseries/ljlm/archive/2026/10.json'])
        for label in builder.WINDOWS:
            product = real_read(self.root / builder.BASE / f'latest_{label}.json')
            self.assertEqual(product['record_count'], 1)

    def test_backfill_idempotence_and_month_placement(self):
        first = sounding('2026-09-30', '12')
        second = sounding()
        second['launch_time'] = '2026-10-01T23:30:00Z'
        self.put('data/2026/09/20260930_12.json', first)
        self.put('data/2026/10/20261002_00.json', second)
        self.put('data/latest.json', second)
        builder.backfill(self.root)
        before = {p: p.read_bytes() for p in (self.root / builder.BASE).rglob('*.json')}
        builder.backfill(self.root)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(len(before), 2)
        self.assertEqual(builder.read_json(self.root / builder.BASE / 'archive/2026/10.json')['record_count'], 1)

    def test_incremental_does_not_scan_sources(self):
        path = self.put('data/latest.json', sounding())
        self.put('data/2000/01/broken.json', {'broken': True})
        with patch.object(Path, 'glob', side_effect=AssertionError('Archive scan')):
            builder.update(self.root, path)
            builder.build_windows(self.root, datetime(2026, 10, 2, tzinfo=timezone.utc))

    def test_recent_sync_recovers_all_terms_and_is_idempotent(self):
        now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        # Seed an older compact record and the latest term; recover the middle.
        old = sounding('2026-09-28', '12')
        latest = sounding('2026-10-02', '12')
        for profile in (old, latest):
            path = self.put('data/latest.json', profile)
            builder.update(self.root, path)
        recent = [('2026-09-29', '12'), ('2026-09-30', '00'),
                  ('2026-09-30', '12'), ('2026-10-01', '00'),
                  ('2026-10-01', '12'), ('2026-10-02', '00'),
                  ('2026-10-02', '12')]
        sources = []
        for date, term in reversed(recent):
            profile = sounding(date, term)
            profile['processed_at'] = '2099-01-01T00:00:00Z'
            profile['parameters'] = {'metpy': {'pwat_mm': None}}
            sources.append(self.put(f'data/{date[:4]}/{date[5:7]}/{date.replace("-", "")}_{term}.json', profile))
        # Malformed old, future, special and unrelated files must never be read.
        for path in ('data/2026/09/20260929_00.json',
                     'data/2026/10/20261003_00.json',
                     'data/1900/01/19000101_00.json',
                     'data/2026/10/20261001_special.json',
                     'data/2026/10/status.json', 'data/2026/10/other.json',
                     'data/latest.json', 'data/status.json'):
            self.put(path, {'unrelated': True})
        real_read = builder.read_json
        accessed = []
        def read(path):
            if path.is_relative_to(self.root / 'data'):
                accessed.append(path)
                self.assertIn(path, sources)
            return real_read(path)
        def run():
            with patch('sys.argv', ['build_dashboard_data.py', '--sync-recent-hours', '72',
                                    '--root', str(self.root), '--as-of', now.isoformat()]), \
                    patch.object(builder, 'read_json', side_effect=read):
                builder.main()
            paths = [*(self.root / builder.BASE).rglob('*.json'),
                     self.root / 'data/dashboard_manifest.json']
            return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        first = run()
        self.assertEqual(set(accessed), set(sources))
        self.assertEqual(first, run())
        for label in builder.WINDOWS:
            records = real_read(self.root / builder.BASE / f'latest_{label}.json')['records']
            self.assertEqual([r['sounding_id'] for r in records],
                             [old['sounding_id']] + [sounding(d, t)['sounding_id'] for d, t in recent])
            self.assertEqual(len({r['sounding_id'] for r in records}), len(records))
            self.assertTrue(all(r['pwat_mm'] is None for r in records))
        manifest = real_read(self.root / 'data/dashboard_manifest.json')
        self.assertEqual(set(manifest['timeseries']['windows']), set(builder.WINDOWS))

    def test_recent_sync_uses_profile_nominal_time_and_regular_terms(self):
        now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        self.put('data/2026/10/20261002_00.json', sounding('2026-09-20'))
        self.put('data/2026/10/20261002_12.json', sounding(term='special'))
        builder.sync_recent(self.root, now, 72)
        self.assertFalse((self.root / builder.BASE).exists())
        for hours in (0, -1):
            with self.assertRaises(ValueError):
                builder.sync_recent(self.root, now, hours)

    def test_manifest_only_existing_products(self):
        empty = builder.build_manifest(self.root)
        self.assertFalse(empty['timeseries']['available'])
        self.assertEqual(empty['icon_d2']['slots'], {})
        self.put('data/latest.json', sounding())
        self.put('timeseries/ljlm/latest_7d.json', {})
        self.put('models/icon-d2/latest/12/sounding.json', {})
        self.put('diagnostics/latest_products.json', {})
        manifest = builder.build_manifest(self.root)
        self.assertNotIn('status', manifest['observations'])
        self.assertEqual(list(manifest['timeseries']['windows']), ['7d'])
        self.assertEqual(list(manifest['icon_d2']['slots']), ['12'])
        def check(value):
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
            elif isinstance(value, str) and '/' in value:
                self.assertTrue((self.root / value).is_file(), value)
        check(manifest)


if __name__ == '__main__':
    unittest.main()
