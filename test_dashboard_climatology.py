import json
from pathlib import Path
import tempfile
import unittest

import build_dashboard_climatology as builder
import build_dashboard_data as dashboard


class CompactClimatologyTests(unittest.TestCase):
    def test_compact_metadata_calendar_null_and_determinism(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = {'station_id': 'SIM00014015', 'station_name': 'Ljubljana',
                        'reference_period': '1996-2025', 'window_description': '±15 calendar days',
                        'smoothing_description': '10-day moving mean', 'created_utc': '2026-10-02',
                        'parameter_metadata': {'t850': {'label': 'T850', 'unit': '°C'},
                                               'missing': {'label': 'Missing'}, '../bad': {}}}
            item = {'n': 10, 'p10': -3, 'p25': -1, 'p50': 0, 'p75': 2, 'p90': 4,
                    'distribution': list(range(10000)), 'historical_min_date': '1900-01-01'}
            source = {'metadata': metadata, 'daily': {'03-01': {'t850': item},
                      '02-29': {'t850': {**item, 'p25': None, 'p90': float('inf')}},
                      '01-01': {'unsupported': item}}}
            dashboard.write_json(root / 'climatology/daily_climatology.json',
                                 {**source, 'daily': {'03-01': {'t850': item}}})
            # Test nonfinite sanitization without allowing it into a normal source writer.
            with (root / 'climatology/daily_climatology.json').open('w') as f:
                json.dump(source, f)
            index = builder.build(root)
            for key in builder.META:
                if key in metadata:
                    self.assertEqual(index[key], metadata[key])
            self.assertEqual([p['key'] for p in index['parameters']], ['t850'])
            path = root / index['parameters'][0]['path']
            result = dashboard.read_json(path)
            self.assertEqual(len(result['days']), 366)
            self.assertEqual([r['day'] for r in result['days']][59:61], ['02-29', '03-01'])
            march = result['days'][60]
            for field in builder.FIELDS:
                self.assertEqual(march[field], item[field])
            self.assertEqual(set(march), {'day', *builder.FIELDS})
            self.assertIsNone(result['days'][59]['p25'])
            self.assertIsNone(result['days'][59]['p90'])
            self.assertIsNone(result['days'][0]['p50'])
            self.assertNotIn('distribution', path.read_text())
            self.assertLess(path.stat().st_size, (root / 'climatology/daily_climatology.json').stat().st_size)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.glob('*.json')}
            builder.build(root)
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
            manifest = dashboard.build_manifest(root)
            self.assertEqual(manifest['climatology'], {'available': True, 'index': 'climatology/dashboard/index.json'})


if __name__ == '__main__':
    unittest.main()
