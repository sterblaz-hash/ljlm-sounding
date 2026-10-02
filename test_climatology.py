"""Offline regression tests for historical surface-parcel Lifted Index."""
from datetime import date
import unittest
from unittest.mock import patch

import numpy as np
import metpy.calc as mpcalc
from metpy.units import units

import build_climatology as climatology
import extract_ljlm as extract


def levels(surface_t=24, surface_td=18, top=300):
    return [dict(pressure_hpa=p, temperature_c=surface_t - (1000-p)*0.065,
                 dewpoint_c=surface_td - (1000-p)*0.075)
            for p in range(1000, top-1, -50)]


class LiftedIndexTests(unittest.TestCase):
    def test_matches_operational_metpy_definition_for_both_signs(self):
        for t0, td0 in ((30, 24), (5, -10)):
            with self.subTest(t0=t0):
                rows = levels(t0, td0)
                p = np.array([r['pressure_hpa'] for r in rows]) * units.hPa
                t = np.array([r['temperature_c'] for r in rows]) * units.degC
                td = np.array([r['dewpoint_c'] for r in rows]) * units.degC
                expected = mpcalc.lifted_index(p, t, mpcalc.parcel_profile(p, t[0], td[0]))
                actual = climatology.calculate_lifted_index(rows)
                self.assertAlmostEqual(actual, float(expected.to('delta_degC').magnitude.item()))
                self.assertLess(actual, 0) if t0 == 30 else self.assertGreater(actual, 0)

    def test_interpolates_500_hpa_and_preserves_zero(self):
        rows = [r for r in levels() if r['pressure_hpa'] != 500]
        p = np.array([r['pressure_hpa'] for r in rows]) * units.hPa
        t = np.array([r['temperature_c'] for r in rows]) * units.degC
        td = np.array([r['dewpoint_c'] for r in rows]) * units.degC
        expected = mpcalc.lifted_index(p, t, mpcalc.parcel_profile(p, t[0], td[0]))
        self.assertAlmostEqual(climatology.calculate_lifted_index(rows),
                               float(expected.to('delta_degC').magnitude.item()))
        with patch.object(climatology.mpcalc, 'lifted_index', return_value=np.array([0.])*units.delta_degC):
            self.assertEqual(climatology.calculate_lifted_index(rows), 0.)

    def test_lowest_common_level_sorting_and_duplicates(self):
        rows = levels()
        rows[0]['dewpoint_c'] = None
        expected = climatology.calculate_lifted_index(rows[1:])
        self.assertIsNotNone(expected)
        self.assertEqual(climatology.calculate_lifted_index(list(reversed(rows)) + [rows[5]]), expected)

    def test_missing_shallow_and_nonfinite_profiles(self):
        for rows in ([], levels(top=550), levels(top=600)[-2:],
                     [dict(pressure_hpa=1000, temperature_c=20, dewpoint_c=None)]):
            self.assertIsNone(climatology.calculate_lifted_index(rows))
        rows = levels()
        for r in rows[:2]:
            r['dewpoint_c'] = None
        self.assertIsNone(climatology.calculate_lifted_index(rows))
        with patch.object(climatology.mpcalc, 'lifted_index', return_value=np.array([np.nan])*units.delta_degC):
            self.assertIsNone(climatology.calculate_lifted_index(levels()))
        with patch.object(climatology.mpcalc, 'parcel_profile', side_effect=ValueError):
            self.assertIsNone(climatology.calculate_lifted_index(levels()))

    def test_profile_qc_and_daily_quality_filter(self):
        day = date(2020, 7, 1)
        for quality in ('good', 'suspect', 'bad'):
            with patch.object(climatology, 'thermodynamic_profile_qc', return_value={
                    'quality': quality, 'passed': quality != 'bad'}):
                record = climatology.calculate_profile_parameters(dict(date=day, hour=0, levels=levels()))
            if quality == 'bad':
                self.assertIsNone(record['lifted_index_c'])
            else:
                self.assertIsNotNone(record['lifted_index_c'])
        records = [dict(date=day, _thermo_quality=q, lifted_index_c=v)
                   for q, v in [('good', -4), ('good', 0), ('suspect', -100), ('bad', 100)]]
        self.assertEqual(climatology.deduplicate_daily(records)[0]['lifted_index_c'], -2)

    def test_statistics_smoothing_and_observational_comparison(self):
        records = [dict(date=date(2020, 7, d), lifted_index_c=v)
                   for d, v in ((1, -4), (2, 0), (3, 5))]
        raw = climatology.build_daily_climatology(records)
        smooth = climatology.smooth_climatology(raw)
        stats = smooth['07-02']['lifted_index_c']
        self.assertEqual(stats['distribution'], [-4, 0, 5])
        self.assertEqual(stats['n'], 3)
        self.assertEqual(stats['min'], -4)
        self.assertEqual(stats['max'], 5)
        self.assertEqual(stats['distribution'], raw['07-02']['lifted_index_c']['distribution'])
        self.assertNotEqual(smooth['06-16']['lifted_index_c']['p50'],
                            raw['06-16']['lifted_index_c']['p50'])
        with patch.object(extract, 'load_climatology_for_date', return_value={'lifted_index_c': stats}):
            result = extract.calculate_climatology_comparison({}, {'lifted_index_c': -2}, date(2026, 7, 2))
        self.assertIn('lifted_index_c', result['parameters'])
        with patch.object(extract, 'load_climatology_for_date', return_value={}):
            result = extract.calculate_climatology_comparison({}, {'lifted_index_c': -2}, date(2026, 7, 2))
        self.assertNotIn('lifted_index_c', result['parameters'])


if __name__ == '__main__':
    unittest.main()
