"""Offline regression tests for compact trajectory history."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
import build_trajectory_data as builder


def profile(date='2026-10-02', term='00', available=True):
    points = [dict(time_s=t, pressure_hpa=990-t/10, height_m=t*5,
                   latitude=46+t/10000, longitude=14+t/10000) for t in range(0, 141, 20)]
    return dict(sounding_id=date.replace('-', '')+'_'+term, nominal_date=date, term=term,
                launch_time='2026-10-01T23:30:00Z', trajectory=dict(available=available, points=points, duration_s=140, max_height_m=700))


class TrajectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def put(self, date, term='00', available=True):
        p = profile(date, term, available)
        path = self.root/'data'/date[:4]/date[5:7]/(date.replace('-', '')+'_'+term+'.json')
        builder.write_json(path, p)
        return path

    def test_decimation_and_unavailable(self):
        source = profile()
        result = builder.extract_record(source)
        self.assertEqual([p['time_s'] for p in result['points']], [0,40,80,120,140])
        self.assertEqual(result['points'][0], source['trajectory']['points'][0])
        self.assertEqual(result['points'][-1], source['trajectory']['points'][-1])
        self.assertEqual(result['duration_s'],140)
        self.assertEqual(result['max_height_m'],700)
        self.assertEqual(result['valid_time'], '2026-10-02T00:00:00Z')
        self.assertIsNone(builder.extract_record(profile(available=False)))
        source['trajectory']['points'][0]['latitude'] = float('nan')
        self.assertEqual(builder.extract_record(source)['points'][0]['time_s'],20)

    def test_backfill_windows_and_manifest(self):
        for date in ('2026-07-01','2026-08-15','2026-09-15','2026-10-02'):
            self.put(date)
        self.put('2026-10-02','12')
        self.put('2026-10-01',available=False)
        builder.backfill(self.root)
        builder.build_windows(self.root,self.now)
        for key, count in [('7d',2),('30d',3),('90d',4)]:
            data=builder.read_json(self.root/builder.BASE/f'latest_{key}.json')
            self.assertEqual(data['record_count'],count)
        first={p:p.read_bytes() for p in (self.root/builder.BASE).rglob('*.json')}
        builder.backfill(self.root)
        builder.build_windows(self.root,self.now)
        self.assertEqual(first,{p:p.read_bytes() for p in first})
        self.assertEqual(len(builder.build_manifest(self.root)['trajectories']['windows']),3)

    def test_recent_sync_recovers_delayed_terms(self):
        self.put('2026-09-29')
        self.put('2026-10-01',available=False)
        builder.sync_recent(self.root,self.now,72)
        self.assertFalse((self.root/builder.BASE/'archive/2026/09.json').exists())
        self.put('2026-10-01',available=True)
        self.put('2026-10-02','12')
        builder.sync_recent(self.root,self.now,72)
        records=builder.read_json(self.root/builder.BASE/'archive/2026/10.json')['records']
        self.assertEqual(len(records),2)
        builder.sync_recent(self.root,self.now,72)
        self.assertEqual(len(builder.read_json(self.root/builder.BASE/'archive/2026/10.json')['records']),2)
        with self.assertRaises(ValueError): builder.sync_recent(self.root,self.now,0)

    def test_update_and_empty_windows(self):
        path=self.put('2026-10-02')
        builder.update(self.root,path)
        builder.update(self.root,path)
        self.assertEqual(builder.read_json(self.root/builder.BASE/'archive/2026/10.json')['record_count'],1)
        builder.build_windows(self.root,datetime(2027,1,1,tzinfo=timezone.utc))
        self.assertEqual(builder.read_json(self.root/builder.BASE/'latest_7d.json')['records'],[])


if __name__ == '__main__':
    unittest.main()
