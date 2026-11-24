"""Offline regression tests for C-LAEF point-cache transient outage handling."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import claef_point_forecast as c


class TransientOutageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.output = Path(self.tempdir.name) / 'point.json'
        self.now = datetime(2026, 11, 24, 8, tzinfo=timezone.utc)

    @staticmethod
    def iso(t):
        return t.isoformat().replace('+00:00', 'Z')

    def product(self, age_hours=4, forecast_hours=48):
        cycle = self.now - timedelta(hours=age_hours)
        rows = [{'valid_time': self.iso(cycle + timedelta(hours=i))}
                for i in range(forecast_hours)]
        return {
            'schema_version': c.SCHEMA_VERSION,
            'model': 'C-LAEF AlpeAdria',
            'requested_location': {'latitude': 46.0656, 'longitude': 14.5122},
            'reference_time': self.iso(cycle),
            'retrieved_at': self.iso(cycle + timedelta(minutes=20)),
            'forecast_start': rows[0]['valid_time'],
            'forecast_end': rows[-1]['valid_time'],
            'forecast_hour_count': len(rows),
            'records': rows,
            'sources': {kind: {'reference_time': self.iso(cycle)} for kind in c.RESOURCES},
        }

    def save(self, product):
        c.atomic_json(self.output, product)
        return self.output.read_bytes()

    def fail(self, *, transient=True):
        client = Mock()
        client.json.side_effect = c.RetrievalError('GeoSphere HTTP 503 after 3 attempt(s).', transient=transient)
        return c.refresh(self.output, client=client, now=self.now)

    def test_recent_503_is_warning_and_keeps_original_data(self):
        original = self.save(self.product())
        result = self.fail()
        self.assertEqual(result['status'], 'degraded')
        self.assertEqual(self.output.read_bytes(), original)
        status = json.loads(self.output.with_name('status.json').read_text())
        self.assertEqual(status['status'], 'warning')
        self.assertTrue(status['previous_forecast_preserved'])
        self.assertIn('HTTP 503', status['message'])

    def test_old_cycle_still_fails_and_marks_error(self):
        original = self.save(self.product(age_hours=13))
        with self.assertRaises(c.RetrievalError):
            self.fail()
        self.assertEqual(original, self.output.read_bytes())
        self.assertEqual(json.loads(self.output.with_name('status.json').read_text())['status'], 'error')

    def test_permanent_http_error_still_fails(self):
        self.save(self.product())
        with self.assertRaises(c.RetrievalError):
            self.fail(transient=False)
        self.assertEqual(json.loads(self.output.with_name('status.json').read_text())['status'], 'error')

    def test_missing_cached_forecast_still_fails(self):
        with self.assertRaises(c.RetrievalError):
            self.fail()
        self.assertFalse(self.output.exists())
        self.assertEqual(json.loads(self.output.with_name('status.json').read_text())['status'], 'error')

    def test_expired_forecast_horizon_still_fails(self):
        self.save(self.product(age_hours=11, forecast_hours=5))
        with self.assertRaises(c.RetrievalError):
            self.fail()

    def test_wrong_source_cycle_still_fails(self):
        product = self.product()
        first_source = next(iter(c.RESOURCES))
        product['sources'][first_source]['reference_time'] = self.iso(self.now)
        self.save(product)
        with self.assertRaises(c.RetrievalError):
            self.fail()


if __name__ == '__main__':
    unittest.main()
