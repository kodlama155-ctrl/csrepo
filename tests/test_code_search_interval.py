import contextlib
import io
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests

from scanner import repository_discovery


CODE_RESULT = {"items": [{"repository": {"full_name": "example/found"}}]}


class CodeSearchIntervalTests(unittest.TestCase):
    def discover(self, memory, api):
        with patch.object(repository_discovery, "load_json", side_effect=lambda path, default: default), \
             patch.object(repository_discovery, "SEARCH_QUERIES", []), \
             patch.object(repository_discovery, "api_get", side_effect=api) as calls, \
             patch.object(time, "sleep"), \
             contextlib.redirect_stdout(io.StringIO()):
            found = repository_discovery.discover_candidates(probe_memory={}, search_memory=memory)
        return found, [call.args[0] for call in calls.call_args_list]

    def test_recent_success_skips_code_search(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=19)).isoformat()
        memory = {"last_success": recent}
        found, calls = self.discover(memory, lambda *args, **kwargs: CODE_RESULT)
        self.assertNotIn("/search/code", calls)
        self.assertEqual(memory, {"last_success": recent})
        self.assertNotIn("example/found", found)

    def test_old_or_missing_success_runs_and_records_search(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=21)).isoformat()
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
        with patch.object(repository_discovery, "load_json", side_effect=lambda path, default: default), \
             patch.object(repository_discovery, "SEARCH_QUERIES", []), \
             patch.object(repository_discovery, "api_get", return_value=CODE_RESULT) as api, \
             patch.object(time, "sleep"):
            found = repository_discovery.discover_candidates()
        self.assertIn("example/found", found)
        api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
