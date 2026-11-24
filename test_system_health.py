"""Offline tests for the read-only LJLM operational health snapshot."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from build_system_health import collect, expected_valid, parse_utc, archive_inventory, action_health, main


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)

    def put(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data), encoding='utf-8')

    def sounding(self, model, slot, run, valid):
        self.put(f'models/{model}/latest/{slot}/sounding.json',
                 {'run_time': run, 'valid_time': valid, 'lead_hours': 12, 'levels': [{'pressure_hpa': 925}]})

    def setup_ok(self):
        self.put('data/status.json', {'terms': {
            '2026-11-24_00': {'nominal_date':'2026-11-24', 'term':'00', 'status':'ok'},
            '2026-11-23_12': {'nominal_date':'2026-11-23', 'term':'12', 'status':'ok'},
        }})
        for model in ('icon-d2', 'claef'):
            self.sounding(model, '00', '2026-11-23T12:00:00Z', '2026-11-24T00:00:00Z')
            self.sounding(model, '12', '2026-11-24T00:00:00Z', '2026-11-24T12:00:00Z')
        self.put('models/claef/latest/status.json', {'status':'ok',
                 'model_reference_time':'2026-11-24T03:00:00Z',
                 'retrieved_at':'2026-11-24T08:46:18Z'})
        self.put('models/claef/latest/point.json', {
            'reference_time':'2026-11-24T03:00:00Z',
            'forecast_start':'2026-11-24T03:00:00Z',
            'forecast_end':'2026-11-24T10:00:00Z',
            'forecast_hour_count':2,
            'records':[{'valid_time':'2026-11-24T03:00:00Z'},
                       {'valid_time':'2026-11-24T10:00:00Z'}]
        })

    def test_all_healthy_when_expected_terms_present(self):
        self.setup_ok()
        report = collect(self.root, self.now)
        self.assertEqual(report['counts']['ok'], 7)

    def test_missing_yesterday_claef_00_is_stale_not_failed_extraction(self):
        self.setup_ok()
        self.sounding('claef', '00', '2026-11-22T12:00:00Z', '2026-11-23T00:00:00Z')
        result = {x['id']: x for x in collect(self.root, self.now)['checks']}
        self.assertEqual(result['claef_sounding_00']['status'], 'stale')
        self.assertEqual(result['claef_sounding_12']['status'], 'ok')
        self.assertEqual(result['claef_sounding_00']['expected_valid'], '2026-11-24T00:00:00Z')

    def test_not_yet_due_12_utc(self):
        self.assertEqual(expected_valid(self.now, '12', 4).isoformat(), '2026-11-23T12:00:00+00:00')

    def test_due_after_cutoff(self):
        clock = datetime(2026,11,24,19,0,tzinfo=timezone.utc)
        self.assertEqual(expected_valid(clock,'12',7).isoformat(), '2026-11-24T12:00:00+00:00')

    def test_missing_json_is_unavailable(self):
        self.setup_ok()
        (self.root/'models/icon-d2/latest/00/sounding.json').unlink()
        result = {x['id']: x for x in collect(self.root,self.now)['checks']}
        self.assertEqual(result['icon_d2_00']['status'], 'unavailable')

    def test_invalid_model_lead_fails(self):
        self.setup_ok()
        self.sounding('claef', '00', '2026-11-23T00:00:00Z', '2026-11-24T00:00:00Z')
        result = {x['id']: x for x in collect(self.root,self.now)['checks']}
        self.assertEqual(result['claef_sounding_00']['status'], 'invalid')

    def test_point_warning_vs_stale(self):
        self.setup_ok()
        self.put('models/claef/latest/status.json', {'status':'warning',
            'model_reference_time':'2026-11-24T03:00:00Z','retrieved_at':'2026-11-24T05:00:00Z'})
        self.assertEqual(collect(self.root,self.now)['checks'][-1]['status'],'warning')
        self.put('models/claef/latest/status.json', {'status':'ok',
            'model_reference_time':'2026-11-23T12:00:00Z','retrieved_at':'2026-11-23T13:00:00Z'})
        self.put('models/claef/latest/point.json', {
            'reference_time':'2026-11-23T12:00:00Z',
            'forecast_start':'2026-11-23T12:00:00Z',
            'forecast_end':'2026-11-25T12:00:00Z',
            'forecast_hour_count':2,
            'records':[{'valid_time':'2026-11-23T12:00:00Z'},
                       {'valid_time':'2026-11-25T12:00:00Z'}]})
        self.assertEqual(collect(self.root,self.now)['checks'][-1]['status'],'stale')

    def test_bad_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_utc('2026-11-24T00:00:00')

    def test_obs_previous_day_12_counts_as_current_before_due(self):
        self.setup_ok()
        result = {x['id']: x for x in collect(self.root,self.now)['checks']}
        self.assertEqual(result['obs_12']['status'],'ok')
        self.assertEqual(result['obs_12']['expected_valid'],'2026-11-23T12:00:00Z')
        self.assertEqual(result['obs_12']['upcoming_valid'],'2026-11-24T12:00:00Z')
        self.assertEqual(result['obs_12']['upcoming_due_at'],'2026-11-24T16:00:00Z')


class HealthV2Tests(unittest.TestCase):
    setUp = HealthTests.setUp
    put = HealthTests.put
    sounding = HealthTests.sounding
    setup_ok = HealthTests.setup_ok
    """New tests preserve the existing v1 cases and cover v2 fields."""

    def put_archive(self, kind, valid, *, broken=False):
        run = valid - timedelta(hours=12)
        rel = f'models/{kind}/{run:%Y%m%d%H}/ljlm_f012.json'
        self.put(rel, {'run_time':run.isoformat().replace('+00:00','Z'),
                       'valid_time': ('2026-11-22T00:00:00Z' if broken
                                      else valid.isoformat().replace('+00:00','Z')),
                       'lead_hours':12, 'levels':[{'pressure_hpa':925}]})

    def test_schema_and_default_actions_are_unchecked(self):
        self.setup_ok()
        r = collect(self.root, self.now)
        self.assertEqual(r['schema_version'], 2)
        self.assertEqual(r['github_actions']['status'], 'not_checked')
        self.assertEqual(len(r['checks']), 7)
        self.assertIn('archive', r)

    def test_archive_gap_maps_valid_to_previous_cycle(self):
        self.setup_ok()
        inventory=archive_inventory(self.root,self.now,1)
        gaps=[g for g in inventory['gaps'] if g['kind']=='claef_sounding'
              and g['valid_time']=='2026-11-24T00:00:00Z']
        self.assertEqual(len(gaps),1)
        self.assertEqual(gaps[0]['file'], 'models/claef/2026112312/ljlm_f012.json')
        self.assertEqual(gaps[0]['expected_run_time'],'2026-11-23T12:00:00Z')
        self.assertEqual(gaps[0]['cause'],'undetermined')

    def test_future_term_not_reported_missing(self):
        items=archive_inventory(self.root,self.now,1)['gaps']
        self.assertFalse(any(g['valid_time']=='2026-11-24T12:00:00Z' for g in items))

    def test_archive_invalid_metadata_not_missing(self):
        self.setup_ok()
        self.put_archive('claef',datetime(2026,11,24,0,tzinfo=timezone.utc),broken=True)
        inventory=archive_inventory(self.root,self.now,1)
        gaps=[x for x in inventory['gaps'] if x['kind']=='claef_sounding'
              and x['valid_time']=='2026-11-24T00:00:00Z']
        self.assertEqual(gaps[0]['issue_code'],'invalid_archive_metadata')

    def test_obs_ok_without_archive_is_gap(self):
        self.setup_ok()
        inventory=archive_inventory(self.root,self.now,1)
        gaps=[x for x in inventory['gaps'] if x['kind']=='obs'
              and x['valid_time']=='2026-11-24T00:00:00Z']
        self.assertEqual(gaps[0]['issue_code'],'archive_file_missing')

    def test_point_json_missing_is_detected_despite_status_ok(self):
        self.setup_ok()
        (self.root/'models/claef/latest/point.json').unlink()
        result=collect(self.root,self.now)['checks'][-1]
        self.assertEqual(result['status'],'unavailable')

    def test_point_json_with_wrong_cycle_is_invalid(self):
        self.setup_ok()
        path='models/claef/latest/point.json'
        v=json.loads((self.root/path).read_text())
        v['reference_time']='2026-11-23T03:00:00Z'
        self.put(path,v)
        self.assertEqual(collect(self.root,self.now)['checks'][-1]['status'],'invalid')

    def test_reported_http_error_is_classified_not_inferred(self):
        self.setup_ok()
        self.put('models/claef/latest/status.json',{
            'status':'warning','model_reference_time':'2026-11-24T03:00:00Z',
            'retrieved_at':'2026-11-24T08:46:18Z','message':'GeoSphere HTTP 503 after attempts'})
        point=collect(self.root,self.now)['checks'][-1]
        self.assertEqual(point['status'],'warning')
        self.assertEqual(point['cause'],'source_http_response_reported')

    def test_actions_latest_success_but_recent_failure_warning(self):
        def run(run_id, date, conclusion):
            return {'id':run_id,'created_at':date,'conclusion':conclusion,'status':'completed',
                    'html_url':f'https://github.com/example/runs/{run_id}'}
        runs={'claef_sounding':[run(12,'2026-11-24T08:00:00Z','success'),
                                 run(11,'2026-11-24T02:00:00Z','failure')]}
        x=action_health(runs,now=self.now)
        c=[i for i in x['workflows'] if i['id']=='claef_sounding'][0]
        self.assertEqual(c['status'],'warning')
        self.assertEqual(c['latest_run']['id'],12)
        self.assertEqual(len(c['recent_failures']),1)

    def test_actions_latest_failed_is_error(self):
        x=action_health({'claef_sounding':[{
            'id':1,'created_at':'2026-11-24T08:30:00Z',
            'status':'completed','conclusion':'failure'}]},now=self.now)
        self.assertEqual(x['status'],'error')

    def test_actions_fetch_error_is_marked_unavailable(self):
        x=action_health({}, {'obs':'HTTPError HTTP 403'}, now=self.now)
        found={i['id']:i for i in x['workflows']}
        self.assertEqual(found['obs']['status'],'unavailable')

    def test_cli_writes_json_without_network(self):
        self.setup_ok()
        from unittest.mock import patch
        with patch('sys.stdout'):
            result=main(['--root',str(self.root),'--as-of','2026-11-24T09:00:00Z'])
        self.assertEqual(result,0)
        output=json.loads((self.root/'data/system_health.json').read_text())
        self.assertEqual(output['schema_version'],2)
        self.assertEqual(output['github_actions']['status'],'not_checked')

    def test_invalid_archive_lookback_rejected(self):
        with self.assertRaises(ValueError):
            archive_inventory(self.root,self.now,32)


    def test_live_github_lookup_is_read_only_and_scoped(self):
        from unittest.mock import patch
        from io import BytesIO
        from build_system_health import fetch_github_runs
        urls=[]
        def fake_open(request, timeout):
            urls.append(request.full_url)
            return BytesIO(json.dumps({'workflow_runs': []}).encode())
        with patch('build_system_health.urlopen', side_effect=fake_open):
            runs, errors=fetch_github_runs()
        self.assertEqual(errors,{})
        self.assertEqual(len(urls),4)
        self.assertTrue(all('/repos/sterblaz-hash/ljlm-sounding/actions/workflows/' in u for u in urls))
        self.assertEqual(len(runs),4)

    def test_point_horizon_expired_not_healthy(self):
        self.setup_ok()
        path='models/claef/latest/point.json'
        v=json.loads((self.root/path).read_text())
        v['forecast_end']='2026-11-24T08:00:00Z'
        v['records'][-1]['valid_time']='2026-11-24T08:00:00Z'
        self.put(path,v)
        self.assertEqual(collect(self.root,self.now)['checks'][-1]['status'],'invalid')


class ArchiveVsCurrentStatusTests(unittest.TestCase):
    """Archive gaps inform history without making recovered streams perpetually red."""
    setUp = HealthTests.setUp
    put = HealthTests.put
    sounding = HealthTests.sounding
    setup_ok = HealthTests.setup_ok

    def test_old_archive_gaps_are_not_current_attention(self):
        self.setup_ok()
        report = collect(self.root, self.now, lookback_days=7)
        self.assertGreater(report['archive']['gap_count'], 0)
        self.assertEqual(report['archive']['status'], 'warning')
        self.assertEqual(report['status'], 'ok')
        self.assertTrue(all(row['status'] == 'ok' for row in report['checks']))

    def test_current_stale_remains_attention_even_with_separate_archive_status(self):
        self.setup_ok()
        self.sounding('claef', '00', '2026-11-22T12:00:00Z', '2026-11-23T00:00:00Z')
        report = collect(self.root, self.now)
        self.assertEqual(report['status'], 'attention')
        self.assertEqual(report['archive']['status'], 'warning')

    def test_failed_action_is_current_attention(self):
        self.setup_ok()
        runs = {'obs': [{'id': 99, 'created_at': '2026-11-24T08:00:00Z',
                         'status': 'completed', 'conclusion': 'failure'}]}
        report = collect(self.root, self.now, action_runs=runs)
        self.assertEqual(report['status'], 'attention')
        self.assertEqual(report['github_actions']['status'], 'error')


if __name__ == '__main__':
    unittest.main()
