import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from requests.adapters import BaseAdapter


SCANNER_PATH = Path(__file__).resolve().parents[1] / "scanner" / "discover.py"
DUMMY_TOKEN = "test-placeholder-not-a-real-token"


class RecordingAdapter(BaseAdapter):
    """Inspect prepared requests without opening any network connections."""

    def __init__(self):
        super().__init__()
        self.requests = []
        self.responses = {}

    def respond(self, url, payload, status=200, headers=None):
        content = payload if isinstance(payload, str) else json.dumps(payload)
        self.responses[url] = (status, content.encode("utf-8"), headers or {})

    def send(self, request, **kwargs):
        self.requests.append(request)
        url = request.url.split("?", 1)[0]
        if url not in self.responses:
            raise AssertionError(f"Unexpected request: {url}")
        status, content, headers = self.responses[url]
        response = requests.Response()
        response.status_code = status
        response._content = content
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        response.headers.update(headers)
        return response

    def close(self):
        pass


class ScannerAuthenticationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("discover_under_test", SCANNER_PATH)
        self.scanner = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"GITHUB_TOKEN": DUMMY_TOKEN}):
            spec.loader.exec_module(self.scanner)
        self.adapter = RecordingAdapter()
        self.scanner.SESSION.trust_env = False
        self.scanner.SESSION.mount("https://", self.adapter)
        self.scanner.SESSION.mount("http://", self.adapter)
        self.addCleanup(self.scanner.SESSION.close)

    def test_api_request_keeps_authentication_and_query(self):
        url = "https://api.github.com/search/repositories"
        self.adapter.respond(url, {"items": []})

        result = self.scanner.api_get("/search/repositories", {"q": "cloudstream"})

        self.assertEqual(result, {"items": []})
        request = self.adapter.requests[0]
        self.assertEqual(request.headers.get("Authorization"), f"Bearer {DUMMY_TOKEN}")
        self.assertIn("q=cloudstream", request.url)

    def test_text_downloads_do_not_receive_token(self):
        for url in (
            "https://cdn.jsdelivr.net/gh/example/repo@main/plugins.json",
            "https://raw.githubusercontent.com/example/repo/builds/repo.json",
            "https://example.invalid/plugins.json",
            "https://api.github.com/repos/example/repo/readme",
        ):
            with self.subTest(url=url):
                self.adapter.respond(url, "[]", headers={"Last-Modified": "test-date"})

                self.assertEqual(self.scanner.text_get(url), ("[]", "test-date"))
                self.assertNotIn("Authorization", self.adapter.requests[-1].headers)

    def test_api_helper_does_not_authenticate_untrusted_addresses(self):
        for url in (
            "https://example.invalid/data",
            "https://api.github.com.example.invalid/data",
            "https://api.github.com@example.invalid/data",
            "http://api.github.com/repos/example/repo",
        ):
            with self.subTest(url=url):
                self.adapter.respond(url, {})

                self.assertEqual(self.scanner.api_get(url), {})
                self.assertNotIn("Authorization", self.adapter.requests[-1].headers)

    def test_api_redirect_does_not_forward_token_to_other_site(self):
        source = "https://api.github.com/repos/example/old"
        destination = "https://example.invalid/redirected"
        self.adapter.respond(source, "", status=302, headers={"Location": destination})
        self.adapter.respond(destination, {"name": "redirected"})

        self.assertEqual(self.scanner.api_get(source), {"name": "redirected"})
        self.assertEqual(self.adapter.requests[0].headers.get("Authorization"), f"Bearer {DUMMY_TOKEN}")
        self.assertNotIn("Authorization", self.adapter.requests[1].headers)

    def test_public_download_redirect_does_not_gain_token(self):
        source = "https://example.invalid/plugins.json"
        destination = "https://api.github.com/repos/example/repo"
        self.adapter.respond(source, "", status=302, headers={"Location": destination})
        self.adapter.respond(destination, "[]")

        self.assertEqual(self.scanner.text_get(source), ("[]", None))
        self.assertTrue(all("Authorization" not in r.headers for r in self.adapter.requests))

    def test_api_without_token_still_works(self):
        self.scanner.TOKEN = ""
        self.adapter.respond("https://api.github.com/repos/example/repo", {"name": "repo"})

        self.assertEqual(self.scanner.api_get("/repos/example/repo"), {"name": "repo"})
        self.assertNotIn("Authorization", self.adapter.requests[0].headers)

    def test_blocked_repository_is_skipped(self):
        self.adapter.respond("https://api.github.com/repos/example/blocked", {"message": "Repository access blocked"}, status=451)

        self.assertIsNone(self.scanner.inspect_repo("example/blocked", {"registry"}))
        self.assertEqual(len(self.adapter.requests), 1)

    def test_temporary_metadata_failure_still_stops_inspection(self):
        self.adapter.respond("https://api.github.com/repos/example/repo", {}, status=503)

        with self.assertRaises(requests.HTTPError):
            self.scanner.inspect_repo("example/repo", {"seed"})

    def test_repository_scan_and_plugin_list_download_still_work(self):
        api_url = "https://api.github.com/repos/example/repo"
        manifest_url = "https://raw.githubusercontent.com/example/repo/builds/repo.json"
        plugins_url = "https://cdn.jsdelivr.net/gh/example/repo@builds/plugins.json"
        plugins = [{"name": "Example", "internalName": "Example", "language": "tr", "status": 1}]
        self.adapter.respond(api_url, {"default_branch": "main", "html_url": "https://github.com/example/repo"})
        self.adapter.respond(api_url + "/branches", [{"name": "builds"}])
        self.adapter.respond(api_url + "/commits", [{"commit": {"committer": {"date": "2026-10-30T00:00:00Z"}}}])
        self.adapter.respond(manifest_url, {"name": "Example", "manifestVersion": 1, "pluginLists": [plugins_url]})
        self.adapter.respond(plugins_url, plugins)

        result = self.scanner.inspect_repo("example/repo", {"seed"})

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["plugin_count"], 1)
        self.assertEqual(result["plugin_lists"][0]["turkish_language_count"], 1)
        self.assertEqual(self.scanner.fetch_plugin_entries(plugins_url), plugins)
        for request in self.adapter.requests:
            if request.url.startswith("https://api.github.com/"):
                self.assertEqual(request.headers.get("Authorization"), f"Bearer {DUMMY_TOKEN}")
            else:
                self.assertNotIn("Authorization", request.headers)


if __name__ == "__main__":
    unittest.main()
