import contextlib
import io
import time
import unittest
from unittest.mock import patch

import requests

from scanner import repository_discovery
from test_discover import DUMMY_TOKEN, RecordingAdapter, use_recording_session


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
        adapter = SequenceAdapter(answers)
        use_recording_session(self, adapter, DUMMY_TOKEN)
        sleeps = []
        self.enterContext(patch.object(time, "sleep", side_effect=sleeps.append))
        return adapter, sleeps

    def call(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                return repository_discovery.api_get("/search/code", {"q": "x"}), output.getvalue()
            except requests.HTTPError as error:
                return error.response.status_code, output.getvalue()

    def test_waits_until_reset_when_quota_is_spent(self):
        reset = str(int(time.time()) + 40)
        spent = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset, "x-ratelimit-resource": "code_search"}
        adapter, sleeps = self.load([(429, spent), (200, {})])
        result, log = self.call()
        self.assertEqual(result["items"][0]["repository"]["full_name"], "a/b")
        self.assertEqual(len(adapter.requests), 2)
        self.assertTrue(39 <= sleeps[0] <= 42, sleeps)
        self.assertEqual(log, "")

    def test_retry_after_header_is_honoured(self):
        adapter, sleeps = self.load([(403, {"retry-after": "7"}), (200, {})])
        self.call()
        self.assertEqual(sleeps, [7])

    def test_long_reset_is_not_waited_and_is_reported(self):
        spent = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 1800),
                 "x-ratelimit-resource": "core", "x-ratelimit-limit": "5000"}
        adapter, sleeps = self.load([(429, spent)])
        status, log = self.call()
        self.assertEqual((status, sleeps, len(adapter.requests)), (429, [], 1))
        self.assertIn("GitHub rate limit: resource=core limit=5000 remaining=0", log)

    def test_permission_403_is_not_reported_as_rate_limit(self):
        adapter, sleeps = self.load([(403, {})] * 3)
        status, log = self.call()
        self.assertEqual((status, sleeps, len(adapter.requests)), (403, [2, 5], 3))
        self.assertNotIn("rate limit", log)


if __name__ == "__main__":
    unittest.main()
