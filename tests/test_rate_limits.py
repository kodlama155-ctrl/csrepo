import contextlib
import importlib.util
import io
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from test_discover import DUMMY_TOKEN, RecordingAdapter


SCANNER_PATH = Path(__file__).resolve().parents[1] / "scanner" / "discover.py"
SEARCH_URL = "https://api.github.com/search/code"


class SequenceAdapter(RecordingAdapter):
    """Answer one URL with a fixed sequence of (status, headers) responses."""

    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)

    def send(self, request, **kwargs):
        status, headers = self.answers.pop(0)
        self.respond(SEARCH_URL, {"items": [{"repository": {"full_name": "a/b"}}]} if status == 200
                     else {"message": "limited"}, status, headers)
        return super().send(request, **kwargs)


class RateLimitTests(unittest.TestCase):
    def load(self, answers):
        spec = importlib.util.spec_from_file_location("rate_limit_under_test", SCANNER_PATH)
        scanner = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"GITHUB_TOKEN": DUMMY_TOKEN}):
            spec.loader.exec_module(scanner)
        adapter = SequenceAdapter(answers)
        scanner.SESSION.trust_env = False
        scanner.SESSION.mount("https://", adapter)
        self.addCleanup(scanner.SESSION.close)
        sleeps = []
        self.enterContext(patch.object(scanner.time, "sleep", side_effect=sleeps.append))
        return scanner, adapter, sleeps

    def call(self, scanner):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                return scanner.api_get("/search/code", {"q": "x"}), output.getvalue()
            except requests.HTTPError as error:
                return error.response.status_code, output.getvalue()

    def test_waits_until_reset_when_quota_is_spent(self):
        reset = str(int(time.time()) + 40)
        spent = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset, "x-ratelimit-resource": "code_search"}
        scanner, adapter, sleeps = self.load([(429, spent), (200, {})])
        result, log = self.call(scanner)
        self.assertEqual(result["items"][0]["repository"]["full_name"], "a/b")
        self.assertEqual(len(adapter.requests), 2)
        self.assertTrue(39 <= sleeps[0] <= 42, sleeps)
        self.assertEqual(log, "")

    def test_retry_after_header_is_honoured(self):
        scanner, adapter, sleeps = self.load([(403, {"retry-after": "7"}), (200, {})])
        self.call(scanner)
        self.assertEqual(sleeps, [7])

    def test_long_reset_is_not_waited_and_is_reported(self):
        spent = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 1800),
                 "x-ratelimit-resource": "core", "x-ratelimit-limit": "5000"}
        scanner, adapter, sleeps = self.load([(429, spent)])
        status, log = self.call(scanner)
        self.assertEqual((status, sleeps, len(adapter.requests)), (429, [], 1))
        self.assertIn("GitHub rate limit: resource=core limit=5000 remaining=0", log)

    def test_permission_403_is_not_reported_as_rate_limit(self):
        scanner, adapter, sleeps = self.load([(403, {})] * 3)
        status, log = self.call(scanner)
        self.assertEqual((status, sleeps, len(adapter.requests)), (403, [2, 5], 3))
        self.assertNotIn("rate limit", log)


if __name__ == "__main__":
    unittest.main()
