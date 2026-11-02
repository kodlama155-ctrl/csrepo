"""Turkish Globalping re-check tests. Every Globalping answer here is a mock.

NOTE for anyone reading the CI log: these tests deliberately simulate failures, so the
"Test scanner" step prints lines such as
    [tr] Globalping cevap vermedi (ConnectionError) ...
    [tr] Globalping durduruldu: kota doldu (HTTP 429) ...
    [tr] Globalping başladı; token: yok / var
They come from mocks, not from the real API. Only "[tr]" lines printed in the
"Scan repositories" step describe the real scan.
"""
import contextlib
import importlib.util
import io
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests


SCANNER_PATH = Path(__file__).resolve().parents[1] / "scanner" / "discover.py"


def site_response(status, body=b""):
    response = requests.Response()
    response.status_code = status
    response._content = body
    return response


def api_response(status, payload=None, headers=None):
    response = Mock(status_code=status, headers=headers or {})
    response.json.return_value = payload or {}
    return response


class TurkishProbeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("tr_probe", SCANNER_PATH)
        self.scanner = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "", "CSREPO_TR_PROBE": "1", "GLOBALPING_TOKEN": ""}):
            spec.loader.exec_module(self.scanner)
        self.addCleanup(self.scanner.SESSION.close)

    def check(self, local_status, answers, url="https://provider.invalid"):
        with patch.object(self.scanner, "query_ttnet_dns", return_value=[]), \
             patch.object(self.scanner.socket, "gethostbyname", return_value="192.0.2.1"), \
             patch.object(self.scanner.requests, "get", return_value=site_response(local_status)), \
             patch.object(self.scanner, "globalping_http", side_effect=answers) as probe:
            failed, reason = self.scanner.is_provider_dead([url], {})
        return failed, reason, probe

    def test_blocked_site_that_opens_from_turkey_is_alive(self):
        failed, reason, probe = self.check(403, [{"status": 200, "location": "", "body": "welcome"}])
        self.assertFalse(failed)
        self.assertEqual(reason, "provider.invalid:alive-tr")
        probe.assert_called_once_with("https://provider.invalid")

    def test_geo_blocked_451_is_also_checked_from_turkey(self):
        failed, reason, _ = self.check(451, [{"status": 200, "location": "", "body": "welcome"}])
        self.assertFalse(failed)

    def test_cloudflare_challenge_from_turkey_is_still_rejected(self):
        failed, reason, _ = self.check(403, [{"status": 200, "location": "", "body": "<title>Just a moment...</title>"}])
        self.assertTrue(failed)
        self.assertIn("http-403", reason)

    def test_parked_domain_from_turkey_is_still_rejected(self):
        failed, _, _ = self.check(403, [{"status": 200, "location": "", "body": "This domain is for sale"}])
        self.assertTrue(failed)

    def test_redirect_is_followed_at_most_twice(self):
        answers = [
            {"status": 301, "location": "https://second.invalid/", "body": ""},
            {"status": 302, "location": "/home", "body": ""},
            {"status": 200, "location": "", "body": "welcome"},
        ]
        failed, _, probe = self.check(403, answers)
        self.assertFalse(failed)
        self.assertEqual([call.args[0] for call in probe.call_args_list],
                         ["https://provider.invalid", "https://second.invalid/", "https://second.invalid/home"])

        endless = [{"status": 301, "location": f"https://hop{index}.invalid/", "body": ""} for index in range(5)]
        failed, _, probe = self.check(403, endless)
        self.assertTrue(failed)
        self.assertEqual(probe.call_count, 3)

    def test_unavailable_probe_keeps_original_rejection(self):
        failed, reason, _ = self.check(403, [None])
        self.assertTrue(failed)
        self.assertIn("http-403", reason)

    def test_other_http_errors_are_not_sent_to_turkey(self):
        failed, _, probe = self.check(404, [])
        self.assertTrue(failed)
        probe.assert_not_called()

    def test_probe_is_off_unless_enabled(self):
        with patch.dict(os.environ, {"CSREPO_TR_PROBE": ""}):
            spec = importlib.util.spec_from_file_location("tr_probe_off", SCANNER_PATH)
            scanner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(scanner)
        self.addCleanup(scanner.SESSION.close)
        with patch.object(scanner.requests, "post") as post:
            self.assertIsNone(scanner.globalping_http("https://provider.invalid"))
        post.assert_not_called()

    def test_measurement_is_requested_from_turkey_and_parsed(self):
        created = api_response(202, {"id": "m1"}, {"x-ratelimit-remaining": "200"})
        finished = api_response(200, {"status": "finished", "results": [{"result": {
            "statusCode": 301, "headers": {"location": ["https://next.invalid/"]}, "rawBody": "Moved",
        }}]})
        with patch.object(self.scanner.requests, "post", return_value=created) as post, \
             patch.object(self.scanner.requests, "get", return_value=finished):
            answer = self.scanner.globalping_http("http://provider.invalid:8080/base?x=1")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["target"], "provider.invalid")
        self.assertEqual(body["locations"], [{"country": "TR"}])
        self.assertEqual(body["measurementOptions"]["protocol"], "HTTP")
        self.assertEqual(body["measurementOptions"]["port"], 8080)
        self.assertEqual(body["measurementOptions"]["request"], {"method": "GET", "path": "/base", "query": "x=1"})
        self.assertEqual(answer, {"status": 301, "location": "https://next.invalid/", "body": "moved"})

    def test_quota_exhaustion_stops_further_measurements(self):
        with patch.object(self.scanner.requests, "post", return_value=api_response(429)) as post:
            self.assertIsNone(self.scanner.globalping_http("https://first.invalid"))
            self.assertIsNone(self.scanner.globalping_http("https://second.invalid"))
        self.assertEqual(post.call_count, 1)

    def load_with_token(self, token):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "", "CSREPO_TR_PROBE": "1", "GLOBALPING_TOKEN": token}):
            spec = importlib.util.spec_from_file_location("tr_probe_token", SCANNER_PATH)
            scanner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(scanner)
        self.addCleanup(scanner.SESSION.close)
        return scanner

    def test_token_is_sent_when_configured(self):
        scanner = self.load_with_token("secret-value")
        with patch.object(scanner.requests, "post", return_value=api_response(500)) as post:
            scanner.globalping_http("https://provider.invalid")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret-value")

    def test_rejected_or_spent_token_falls_back_to_anonymous_once(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                scanner = self.load_with_token("secret-value")
                output = io.StringIO()
                with patch.object(scanner.requests, "post", side_effect=[api_response(status), api_response(500), api_response(500)]) as post, \
                     contextlib.redirect_stdout(output):
                    scanner.globalping_http("https://provider.invalid")
                    scanner.globalping_http("https://second.invalid")
                first, retry, *later = post.call_args_list
                self.assertIn("Authorization", first.kwargs["headers"])
                self.assertNotIn("Authorization", retry.kwargs["headers"])
                self.assertTrue(all("Authorization" not in call.kwargs["headers"] for call in later))
                self.assertNotIn("secret-value", output.getvalue())

    def test_first_measurement_logs_token_presence_once_without_value(self):
        for token, expected in (("secret-value", "token: var"), ("", "token: yok")):
            with self.subTest(token=bool(token)):
                scanner = self.load_with_token(token)
                output = io.StringIO()
                with patch.object(scanner.requests, "post", return_value=api_response(500)), \
                     contextlib.redirect_stdout(output):
                    scanner.globalping_http("https://first.invalid")
                    scanner.globalping_http("https://second.invalid")
                self.assertEqual(output.getvalue().count(expected), 1)
                self.assertNotIn("secret-value", output.getvalue())

    def test_no_token_sends_no_authorization(self):
        with patch.object(self.scanner.requests, "post", return_value=api_response(500)) as post:
            self.scanner.globalping_http("https://provider.invalid")
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_api_failure_means_no_answer(self):
        with patch.object(self.scanner.requests, "post", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(self.scanner.globalping_http("https://provider.invalid"))


if __name__ == "__main__":
    unittest.main()
