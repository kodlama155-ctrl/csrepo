import contextlib
import importlib.util
import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests


SCANNER_PATH = Path(__file__).resolve().parents[1] / "scanner" / "discover.py"
CODE_RESULT = {"items": [{"repository": {"full_name": "example/found"}}]}


class CodeSearchIntervalTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("code_search_under_test", SCANNER_PATH)
        self.scanner = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            spec.loader.exec_module(self.scanner)
        self.addCleanup(self.scanner.SESSION.close)

    def discover(self, memory, api):
        with patch.object(self.scanner, "load_json", side_effect=lambda path, default: default), \
             patch.object(self.scanner, "SEARCH_QUERIES", []), \
             patch.object(self.scanner, "api_get", side_effect=api) as calls, \
             patch.object(self.scanner.time, "sleep"), \
             contextlib.redirect_stdout(io.StringIO()):
            found = self.scanner.discover_candidates(probe_memory={}, search_memory=memory)
        return found, [call.args[0] for call in calls.call_args_list]

    def test_recent_success_skips_code_search(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat()
        memory = {"last_success": recent}
        found, calls = self.discover(memory, lambda *args, **kwargs: CODE_RESULT)
        self.assertNotIn("/search/code", calls)
        self.assertEqual(memory, {"last_success": recent})
        self.assertNotIn("example/found", found)

    def test_old_or_missing_success_runs_and_records_search(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        for memory in ({"last_success": old}, {}):
            with self.subTest(memory=dict(memory)):
                found, calls = self.discover(memory, lambda *args, **kwargs: CODE_RESULT)
                self.assertIn("/search/code", calls)
                self.assertIn("example/found", found)
                self.assertNotEqual(memory.get("last_success"), old)
                self.assertIsNotNone(memory.get("last_success"))

    def test_failed_search_is_retried_next_scan(self):
        def fail(*args, **kwargs):
            raise requests.HTTPError("429 Too Many Requests")
        memory = {}
        self.discover(memory, fail)
        self.assertEqual(memory, {})

    def test_without_memory_code_search_always_runs(self):
        with patch.object(self.scanner, "load_json", side_effect=lambda path, default: default), \
             patch.object(self.scanner, "SEARCH_QUERIES", []), \
             patch.object(self.scanner, "api_get", return_value=CODE_RESULT) as api, \
             patch.object(self.scanner.time, "sleep"):
            found = self.scanner.discover_candidates()
        self.assertIn("example/found", found)
        api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
