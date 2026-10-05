"""Offline regressions for delayed LJLM search windows."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import unittest
from unittest.mock import patch

import extract_ljlm as extract


class SearchWindowTests(unittest.TestCase):
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

    def entry(self, age, status="missing", term=None):
        nominal = self.now - timedelta(hours=age)
        return {
            "nominal_date": nominal.date().isoformat(),
            "term": term or nominal.strftime("%H"),
            "status": status,
            # Recent status updates must not make an old nominal term eligible.
            "missing_since": self.now.isoformat(),
            "checked_at": self.now.isoformat(),
        }

    def check_search(self, event, entries, mode, hours):
        ages = [1, 3, 3.01, 24, 48, 48.01, 60, 72, 72.01]
        files = {
            age: "Z__C_EDZW_" + (self.now - timedelta(hours=age)).strftime(
                "%Y%m%d%H%M%S"
            ) + "_temp_bufr.bin"
            for age in ages
        }
        html = "".join(f'<a href="{name}">package</a>' for name in files.values())
        output = StringIO()
        with patch.dict(extract.os.environ, {"GITHUB_EVENT_NAME": event}), \
                patch.object(extract, "load_status_file", return_value={
                    "terms": {str(i): entry for i, entry in enumerate(entries)}
                }), patch.object(extract, "get_directory_listing", return_value=html), \
                redirect_stdout(output):
            selected = extract.find_candidate_files(now=self.now)
        self.assertEqual(set(selected), {name for age, name in files.items() if age <= hours})
        self.assertIn(f"Search mode: {mode}\n", output.getvalue())
        self.assertIn(f"DWD search window: {hours} h\n", output.getvalue())

    def test_normal_scheduled(self):
        for entries in ([], [self.entry(12, "ok")], [self.entry(12, "waiting")],
                        [self.entry(60)], [self.entry(12, term="special")],
                        [self.entry(-12)]):
            with self.subTest(entries=entries):
                self.check_search("schedule", entries, "scheduled", 3)

    def test_scheduled_catch_up_for_either_regular_term(self):
        for age in (12, 24, 48):
            with self.subTest(age=age):
                self.check_search("schedule", [self.entry(60), self.entry(age)],
                                  "scheduled catch-up", 48)

    def test_missing_term_just_older_than_48_hours(self):
        with patch.dict(extract.os.environ, {"GITHUB_EVENT_NAME": "schedule"}), \
                patch.object(extract, "load_status_file", return_value={
                    "terms": {"old": self.entry(48)}
                }):
            self.assertEqual(extract.dwd_search_mode(self.now + timedelta(seconds=1)),
                             ("scheduled", 3))

    def test_manual_always_uses_72_hours(self):
        for entries in ([], [self.entry(12)], [self.entry(60)]):
            with self.subTest(entries=entries):
                self.check_search("workflow_dispatch", entries, "manual catch-up", 72)


if __name__ == "__main__":
    unittest.main()
