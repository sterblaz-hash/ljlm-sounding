"""Offline tests for long-term LJLM model-error archives and summaries."""
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_model_verification_data as b


END = '2026-10-12T12:00:00Z'


def comparison(stamp=END, error=2):
    valid = b.utc_time(stamp)
    return {
        'version': 1, 'station': 14015, 'valid_time': stamp,
        'observation_sounding_id': valid.strftime('%Y%m%d_%H'),
        'models': {model: {'run_time': b.iso(valid - timedelta(hours=12)), 'lead_hours': 12}
                   for model in b.MODELS},
        'standard_pressure_comparisons': [
            {'pressure_hpa': p, 'values': {'OBS': {'temperature_c': 999}},
             'model_minus_obs': {
                 model: {metric: abs(error) if metric == 'wind_vector_error_ms' else
                         error if model == 'ICON-D2' else -error for metric in b.METRICS}
                 for model in b.MODELS}}
            for p in reversed(b.PRESSURES)],
    }


class ExtractionTests(unittest.TestCase):
    def test_model_errors_order_and_metadata(self):
        record = b.extract_record(comparison())
        self.assertEqual(list(record['errors']), list(b.MODELS))
        self.assertEqual(list(record['errors']['ICON-D2']), ['925', '850', '700', '500', '300'])
        self.assertEqual(record['errors']['ICON-D2']['925']['temperature_c'], 2)
        self.assertEqual(record['errors']['C-LAEF']['925']['temperature_c'], -2)
        self.assertEqual(record['verification_date'], '2026-10-12')
        self.assertEqual(record['term'], '12')
        self.assertEqual(record['run_times'], {model: '2026-10-12T00:00:00Z' for model in b.MODELS})
        self.assertEqual(record['valid_time'], END)
        self.assertEqual(record['observation_sounding_id'], '20261012_12')

    def test_null_missing_metrics_and_pressures(self):
        source = comparison()
        source['standard_pressure_comparisons'] = source['standard_pressure_comparisons'][:-1]
        values = source['standard_pressure_comparisons'][0]['model_minus_obs']['ICON-D2']
        values['temperature_c'] = None
        del values['dewpoint_c']
        record = b.extract_record(source)
        self.assertTrue(all(value is None for value in record['errors']['ICON-D2']['925'].values()))
        self.assertIsNone(record['errors']['ICON-D2']['300']['temperature_c'])
        self.assertIsNone(record['errors']['ICON-D2']['300']['dewpoint_c'])

    def test_utc_exact_times(self):
        for stamp in ('2026-10-12T00:00Z', '2026-10-12T12:00+00:00'):
            self.assertEqual(b.extract_record(comparison(stamp))['term'], stamp[11:13])
        for stamp in ('2026-10-12T12:00', '2026-10-12T12:00+02:00', '2026-10-12T06:00Z',
                      '2026-10-12T00:01Z', '2026-10-12T00:00:01Z', '2026-10-12T12:00:00.001Z'):
            source = comparison()
            source['valid_time'] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                b.extract_record(source)

    def test_strict_schema(self):
        for key in ('version', 'valid_time', 'observation_sounding_id', 'models', 'standard_pressure_comparisons'):
            source = comparison(); del source[key]
            with self.subTest(key=key), self.assertRaises(ValueError): b.extract_record(source)
        for model in b.MODELS:
            source = comparison(); del source['models'][model]
            with self.assertRaises(ValueError): b.extract_record(source)
            source = comparison(); source['models'][model]['lead_hours'] = 6
            with self.assertRaises(ValueError): b.extract_record(source)
            source = comparison(); source['models'][model]['run_time'] = END
            with self.assertRaises(ValueError): b.extract_record(source)
        source = comparison(); source['standard_pressure_comparisons'].append(source['standard_pressure_comparisons'][0])
        with self.assertRaises(ValueError): b.extract_record(source)
        for value in (float('nan'), float('inf'), -float('inf'), True, '2', 10**1000):
            source = comparison(); source['standard_pressure_comparisons'][0]['model_minus_obs']['ICON-D2']['u_ms'] = value
            with self.subTest(value=str(value)[:10]), self.assertRaises(ValueError): b.extract_record(source)
        source = comparison(); source['standard_pressure_comparisons'][0]['model_minus_obs']['ICON-D2']['wind_vector_error_ms'] = -1
        with self.assertRaises(ValueError): b.extract_record(source)

    def test_statistics_scalar_vector_and_null_counts(self):
        records = [b.extract_record(comparison(error=value)) for value in (2, -4, 0)]
        records[2]['errors']['ICON-D2']['925']['temperature_c'] = None
        stats = b.statistics(records)
        signed = stats['ICON-D2']['925']['temperature_c']
        self.assertEqual(signed['n'], 2)
        self.assertEqual(signed['bias'], -1)
        self.assertEqual(signed['mae'], 3)
        self.assertAlmostEqual(signed['rmse'], math.sqrt(10))
        vector = stats['ICON-D2']['925']['wind_vector_error_ms']
        self.assertNotIn('bias', vector)
        self.assertEqual(vector['n'], 3)
        self.assertEqual(vector['mae'], 2)
        self.assertAlmostEqual(vector['rmse'], math.sqrt(20 / 3))
        self.assertEqual(stats['C-LAEF']['925']['temperature_c']['n'], 3)
        empty = b.statistics([])
        self.assertEqual(empty['ICON-D2']['925']['u_ms'], {'n': 0, 'bias': None, 'mae': None, 'rmse': None})
        self.assertEqual(empty['ICON-D2']['925']['wind_vector_error_ms'], {'n': 0, 'mae': None, 'rmse': None})

    def test_finite_large_values_do_not_overflow_statistics(self):
        records = [b.extract_record(comparison(error=1e308)), b.extract_record(comparison(error=-1e308))]
        stats = b.statistics(records)['ICON-D2']['925']['temperature_c']
        self.assertEqual(stats, {'n': 2, 'bias': 0, 'mae': 1e308, 'rmse': 1e308})
        json.dumps(stats, allow_nan=False)

    def test_duplicate_valid_time_last_update_wins(self):
        first = b.extract_record(comparison('2026-10-12T00:00Z'))
        second = b.extract_record(comparison('2026-10-12T00:00:00+00:00', error=4))
        later = b.extract_record(comparison())
        records = b.sorted_records([later, first, second])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['errors']['ICON-D2']['925']['temperature_c'], 4)
        self.assertLess(records[0]['valid_time'], records[1]['valid_time'])


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, stamp=END, error=2):
        valid = b.utc_time(stamp)
        path = self.root / b.SOURCE / valid.strftime('%Y/%m/%Y%m%d_%H_comparison.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(comparison(stamp, error)), encoding='utf-8')
        return path

    def output(self, relative):
        return json.loads((self.root / b.BASE / relative).read_text())

    def snapshot(self):
        return {path.relative_to(self.root): path.read_bytes() for path in
                (self.root / b.BASE).rglob('*') if path.is_file()}

    def test_update_deduplication_and_idempotence(self):
        source = self.source()
        b.build(self.root, update=source, as_of=END)
        original = self.snapshot()
        with patch.object(b.os, 'replace') as replace:
            b.build(self.root, update=source, as_of=END)
            replace.assert_not_called()
        self.assertEqual(original, self.snapshot())
        self.source(error=4)
        b.build(self.root, update=source, as_of=END)
        archive = self.output('archive/2026/10.json')
        self.assertEqual(archive['record_count'], 1)
        self.assertEqual(archive['records'][0]['errors']['ICON-D2']['925']['u_ms'], 4)
        self.assertNotIn('generated_at', archive)

    def test_window_boundaries_order_and_determinism(self):
        end = b.utc_time(END)
        for days in (0, 7, 30, 90, 365, 366):
            self.source(b.iso(end - timedelta(days=days)))
        self.source(b.iso(end + timedelta(hours=12)))
        b.build(self.root, backfill=True, as_of=END)
        for label, count in [('7d', 2), ('30d', 3), ('90d', 4), ('1y', 5)]:
            window = self.output(f'latest_{label}.json')
            self.assertEqual(window['record_count'], count)
            self.assertEqual(window['end_time'], END)
            self.assertEqual(window['generated_at'], END)
            self.assertEqual(window['start_time'], b.iso(end - timedelta(days=b.WINDOWS[label])))
            times = [row['valid_time'] for row in window['records']]
            self.assertEqual(times, sorted(times))
            self.assertEqual(window['statistics']['ICON-D2']['925']['temperature_c']['n'], count)
        previous = self.snapshot()
        b.build(self.root, backfill=True, as_of=END)
        self.assertEqual(previous, self.snapshot())

    def test_backfill_multiple_months_ignores_unrelated_files(self):
        paths = [self.source(stamp) for stamp in ('2026-09-30T12:00Z', END)]
        source_bytes = {path: path.read_bytes() for path in paths}
        latest = self.root / b.SOURCE / 'latest/00/comparison.json'
        latest.parent.mkdir(parents=True); latest.write_text('{')
        unrelated = paths[0].parent / '20260930_06_comparison.json'; unrelated.write_text('{')
        (paths[0].parent / '20260930_12_skewt_overlay.png').write_bytes(b'PNG')
        (paths[0].parent / 'unrelated.json').write_text('{')
        skipped = b.build(self.root, backfill=True, as_of=END)
        self.assertEqual(skipped, [])
        self.assertEqual(self.output('archive/2026/09.json')['record_count'], 1)
        self.assertEqual(self.output('archive/2026/10.json')['record_count'], 1)
        self.assertEqual(self.output('latest_30d.json')['record_count'], 2)
        self.assertEqual(source_bytes, {path: path.read_bytes() for path in paths})
        self.assertTrue(all(path.is_relative_to(b.BASE) for path in self.snapshot()))

    def test_bad_update_preserves_all_outputs(self):
        source = self.source(); b.build(self.root, update=source, as_of=END)
        previous = self.snapshot()
        for invalid in ('{', '{}', source.read_text().replace('2,', 'NaN,', 1)):
            source.write_text(invalid)
            with self.assertRaises(ValueError): b.build(self.root, update=source, as_of=END)
            self.assertEqual(previous, self.snapshot())

    def test_backfill_bad_source_preserves_valid_data_and_merges_good(self):
        first = self.source('2026-09-30T12:00Z')
        b.build(self.root, update=first, as_of=END)
        first.write_text('{')
        self.source()
        skipped = b.build(self.root, backfill=True, as_of=END)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(self.output('archive/2026/09.json')['record_count'], 1)
        self.assertEqual(self.output('latest_30d.json')['record_count'], 2)

    def test_atomic_replace_failure_preserves_destination_and_cleans_temp(self):
        source = self.source(); b.build(self.root, update=source, as_of=END)
        previous = self.snapshot(); self.source(error=4)
        with patch.object(b.os, 'replace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError): b.build(self.root, update=source, as_of=END)
        self.assertEqual(previous, self.snapshot())
        self.assertEqual(list((self.root / b.BASE).rglob('*.tmp')), [])
        original = os.replace
        def check_before_replace(src, dst):
            self.assertEqual(Path(src).parent, Path(dst).parent)
            json.loads(Path(src).read_text())
            self.assertEqual(Path(dst).read_bytes(), previous[Path(dst).relative_to(self.root)])
            original(src, dst)
        with patch.object(b.os, 'replace', side_effect=check_before_replace) as replace:
            b.build(self.root, update=source, as_of=END)
        self.assertEqual(replace.call_count, 5)

    def test_recent_recovery_inclusive_window_and_future_exclusion(self):
        for stamp in ('2026-10-11T00:00Z', '2026-10-11T12:00Z', '2026-10-12T00:00Z', END, '2026-10-13T00:00Z'):
            self.source(stamp)
        old = self.root / b.SOURCE / '2026/10/20261011_00_comparison.json'; old.write_text('{')
        self.assertEqual(b.build(self.root, recent_hours=24, as_of=END), [])
        self.assertEqual(self.output('latest_7d.json')['record_count'], 3)
        for hours in (0, -1, 1.5):
            with self.assertRaises(ValueError): b.build(self.root, recent_hours=hours, as_of=END)

    def test_no_nan_infinity_and_null_statistics(self):
        source = self.source()
        data = comparison()
        for row in data['standard_pressure_comparisons']:
            row['model_minus_obs']['ICON-D2']['temperature_c'] = None
        source.write_text(json.dumps(data))
        b.build(self.root, update=source, as_of=END)
        for content in self.snapshot().values():
            text = content.decode()
            self.assertNotIn('NaN', text); self.assertNotIn('Infinity', text)
            json.loads(text, parse_constant=lambda value: self.fail(value))
        self.assertEqual(self.output('latest_7d.json')['statistics']['ICON-D2']['925']['temperature_c']['n'], 0)

    def test_symlinks_refused_and_not_followed(self):
        source = self.source()
        other = self.root / 'outside'; other.mkdir()
        link = source.parent / '20261012_00_comparison.json'
        target = other / 'comparison.json'; target.write_text(json.dumps(comparison('2026-10-12T00:00Z')))
        link.symlink_to(target)
        with self.assertRaises(ValueError): b.build(self.root, update=link, as_of=END)
        b.build(self.root, backfill=True, as_of=END)
        self.assertEqual(self.output('latest_7d.json')['record_count'], 1)
        latest = self.root / b.BASE / 'latest_7d.json'
        previous = target.read_bytes(); latest.unlink(); latest.symlink_to(target)
        self.source(error=4)
        snapshot = self.snapshot()
        with self.assertRaises(ValueError): b.build(self.root, update=source, as_of=END)
        self.assertEqual(previous, target.read_bytes())
        self.assertEqual(snapshot, self.snapshot())

    def test_symlinked_output_ancestor_and_source_directory(self):
        source = self.source()
        outside = self.root / 'outside'; outside.mkdir()
        (self.root / b.BASE).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): b.build(self.root, update=source, as_of=END)
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / b.BASE).unlink()
        linked_year = self.root / b.SOURCE / '2025'; linked_year.symlink_to(outside, target_is_directory=True)
        self.assertEqual(b.build(self.root, backfill=True, as_of=END), [])
        with self.assertRaises(ValueError): b.safe_path(linked_year / '01/nonexistent.json')

    def test_filename_mismatch_unrelated_update_and_corrupt_archive(self):
        source = self.source()
        source.write_text(json.dumps(comparison('2026-10-12T00:00Z')))
        with self.assertRaises(ValueError): b.build(self.root, update=source, as_of=END)
        unrelated = self.root / 'unrelated.json'; unrelated.write_text(json.dumps(comparison()))
        with self.assertRaises(ValueError): b.build(self.root, update=unrelated, as_of=END)
        self.source(); b.build(self.root, update=source, as_of=END)
        archive = self.root / b.BASE / 'archive/2026/10.json'; archive.write_text('{')
        previous = self.snapshot()
        with self.assertRaises(ValueError): b.build(self.root, update=source, as_of=END)
        self.assertEqual(previous, self.snapshot())

    def test_output_paths_cannot_escape_timeseries(self):
        for relative in (Path('outside.json'), b.BASE / '../outside.json',
                         b.BASE / '../../../outside.json', self.root / 'outside.json'):
            with self.subTest(path=relative), self.assertRaises(ValueError):
                b.output_path(self.root, relative)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_modes_and_as_of(self):
        source = self.source()
        b.main(['--root', str(self.root), '--update', str(source.relative_to(self.root)), '--as-of', END])
        b.main(['--root', str(self.root), '--backfill', '--as-of', END])
        b.main(['--root', str(self.root), '--sync-recent-hours', '24', '--as-of', END])
        previous = self.snapshot()
        with self.assertRaises(SystemExit):
            b.main(['--root', str(self.root), '--backfill', '--as-of', '2026-10-12T00:00'])
        self.assertEqual(previous, self.snapshot())


if __name__ == '__main__':
    unittest.main()
