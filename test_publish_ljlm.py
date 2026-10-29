"""Offline tests of generated-product publication and racing GitHub pushes."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import publish_ljlm


class FakeRunner:
    def __init__(self, pushes=(), dirty=True):
        self.calls = []
        self.pushes = list(pushes)
        self.dirty = dirty

    def __call__(self, argv, *, allow_failure=False):
        self.calls.append(list(argv))
        if argv == ["git", "diff", "--cached", "--quiet"]:
            return 1 if self.dirty else 0
        if argv[:2] == ["git", "push"]:
            return self.pushes.pop(0)
        return 0

    def count(self, argv):
        return self.calls.count(argv)


class TestSafeLjlmPublishing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.temp.name)
        Path("data").mkdir()
        Path("data/latest.json").write_text("{}")
        Path("verification").mkdir()

    def tearDown(self):
        os.chdir(self.cwd)
        self.temp.cleanup()

    def test_first_attempt_succeeds(self):
        fake = FakeRunner([0])
        self.assertTrue(publish_ljlm.publish(fake))
        self.assertEqual(fake.count(["git", "reset", "--hard", "origin/main"]), 1)
        self.assertEqual(fake.count(["git", "push", "origin", "HEAD:main"]), 1)
        self.assertFalse(any("--rebase" in call or "--force" in call for call in fake.calls))

    def test_rejected_push_rebuilds_from_fresh_main(self):
        fake = FakeRunner([1, 0])
        self.assertTrue(publish_ljlm.publish(fake))
        self.assertEqual(fake.count(["git", "reset", "--hard", "origin/main"]), 2)
        self.assertEqual(fake.count(["git", "fetch", "--no-tags", "origin", "main"]), 2)
        self.assertEqual(fake.count(["git", "push", "origin", "HEAD:main"]), 2)
        self.assertEqual(fake.count(["git", "commit", "-m", "Update LJLM sounding"]), 2)
        self.assertEqual(fake.count(list(publish_ljlm.PIPELINE[0][1])), 2)
        self.assertFalse(any("--rebase" in call or "--force" in call for call in fake.calls))

    def test_no_difference_does_not_push(self):
        fake = FakeRunner(dirty=False)
        self.assertFalse(publish_ljlm.publish(fake))
        self.assertFalse(any(call[:2] == ["git", "push"] for call in fake.calls))

    def test_failure_after_three_pushes_is_explicit(self):
        fake = FakeRunner([1, 1, 1])
        with self.assertRaisesRegex(RuntimeError, "no force push"):
            publish_ljlm.publish(fake)
        self.assertEqual(fake.count(["git", "reset", "--hard", "origin/main"]), 3)

    def test_never_runs_as_local_user(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false", "GITHUB_REF": "refs/heads/main"}):
            self.assertEqual(publish_ljlm.main(), 2)
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/feature"}):
            self.assertEqual(publish_ljlm.main(), 2)

    def test_stage_json_only_with_explicit_scope(self):
        Path("data/raw.bin").write_bytes(b"ignore")
        Path("data/a.json").write_text("{}")
        fake = FakeRunner()
        publish_ljlm.stage_products(fake)
        args = " ".join(" ".join(call) for call in fake.calls)
        self.assertIn("data/latest.json", args)
        self.assertIn("data/a.json", args)
        self.assertNotIn("raw.bin", args)
        self.assertIn("verification", args)


if __name__ == "__main__":
    unittest.main()
