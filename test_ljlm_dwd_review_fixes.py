"""Additional regression tests for PR #18 code-review fixes (offline)."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ljlm_dwd_fastpath as fast


class NegativeCacheReviewTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 3, 14, tzinfo=timezone.utc)
        self.filename_time = self.now - timedelta(hours=1)
        self.cached = {'contains_ljlm':False,
                       'scanned_at': (self.now-timedelta(minutes=20)).isoformat()}

    def test_recent_negative_skipped_for_short_time(self):
        self.assertTrue(fast.can_skip_negative(self.cached,self.filename_time,self.now))

    def test_recent_negative_rechecked_after_ttl(self):
        self.cached['scanned_at']=(self.now-timedelta(minutes=91)).isoformat()
        self.assertFalse(fast.can_skip_negative(self.cached,self.filename_time,self.now))

    def test_old_negative_kept_for_fast_mode(self):
        self.assertTrue(fast.can_skip_negative(self.cached,
            self.now-timedelta(hours=10),self.now))

    def test_corrupt_cache_never_skipped(self):
        self.assertFalse(fast.can_skip_negative({'contains_ljlm':False,'scanned_at':'invalid'},
             self.filename_time,self.now))


class GraphRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/'data').mkdir()
        (self.root/'diagnostics').mkdir()
        self.profile={'sounding_id':'20261103_00'}
        self.paths={
           'skewt':'diagnostics/latest_skewt.png',
           'skewt_zoom':'diagnostics/latest_skewt_zoom.png',
           'lowlevel':'diagnostics/latest_lowlevel.png',
           'thetae':'diagnostics/latest_thetae.png',
           'hodograph':'diagnostics/latest_hodograph.png',
        }
        self.manifest={**self.paths,'sounding_id':'20261103_00',
                       'rendered_at':'2026-11-03T07:22:38Z'}
        (self.root/'data/latest.json').write_text(json.dumps(self.profile))
        (self.root/'diagnostics/latest_products.json').write_text(json.dumps(self.manifest))
        for filename in self.paths.values():
            (self.root/filename).write_bytes(b'PNG')

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_graphs_skip_rebuild(self):
        self.assertTrue(fast.published_latest_graphs_current(self.root))

    def test_missing_graph_forces_recovery(self):
        (self.root/self.paths['skewt']).unlink()
        self.assertFalse(fast.published_latest_graphs_current(self.root))

    def test_old_manifest_forces_recovery(self):
        self.manifest['sounding_id']='20261102_12'
        (self.root/'diagnostics/latest_products.json').write_text(json.dumps(self.manifest))
        self.assertFalse(fast.published_latest_graphs_current(self.root))


class PublisherReviewTests(unittest.TestCase):
    def test_fast_scan_no_change_skips_only_with_valid_graphs(self):
        import publish_ljlm
        from test_publish_ljlm import FakeRunner
        with tempfile.TemporaryDirectory() as dirname:
            import os
            prior=os.getcwd()
            try:
                os.chdir(dirname)
                Path('data').mkdir()
                Path('data/latest.json').write_text('{}')
                Path('verification').mkdir()
                with patch.dict(os.environ, {'LJLM_SCAN_MODE':'fast'}), \
                     patch.object(publish_ljlm,'marker_has_updates',return_value=False), \
                     patch.object(publish_ljlm,'published_latest_graphs_current',return_value=False):
                    fake=FakeRunner(dirty=False)
                    publish_ljlm.publish(fake)
                    self.assertEqual(fake.count(list(publish_ljlm.PIPELINE[-1][1])),1)
                with patch.dict(os.environ, {'LJLM_SCAN_MODE':'fast'}), \
                     patch.object(publish_ljlm,'marker_has_updates',return_value=False), \
                     patch.object(publish_ljlm,'published_latest_graphs_current',return_value=True):
                    fake=FakeRunner(dirty=False)
                    publish_ljlm.publish(fake)
                    self.assertEqual(fake.count(list(publish_ljlm.PIPELINE[-1][1])),0)
            finally:
                os.chdir(prior)


if __name__=='__main__':
    unittest.main()
