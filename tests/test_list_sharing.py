import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import requests

from test_discover import RecordingAdapter


SCANNER_PATH = Path(__file__).resolve().parents[1] / "scanner" / "discover.py"
SHARED_URL = "https://raw.githubusercontent.com/source/shared/builds/plugins.json"


class ListSharingTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("list_sharing_under_test", SCANNER_PATH)
        self.scanner = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            spec.loader.exec_module(self.scanner)
        self.adapter = RecordingAdapter()
        self.scanner.SESSION.trust_env = False
        self.scanner.SESSION.mount("https://", self.adapter)
        self.scanner.SESSION.mount("http://", self.adapter)
        self.addCleanup(self.scanner.SESSION.close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scanner.ROOT = Path(temporary.name)
        self.scanner.DATA = self.scanner.ROOT / "data"
        self.scanner.save_json(self.scanner.DATA / "seeds.json", [{"repository": "example/repo-a"}])
        self.plugin = {"name": "Example", "internalName": "Example", "language": "tr", "status": 1,
                       "version": 1, "url": "https://example.invalid/Example.cs3"}
        self.adapter.respond(SHARED_URL, [self.plugin])
        self.now = datetime.now(timezone.utc).isoformat()
        self.adapter.respond("https://api.github.com/repos/source/shared/commits", [
            {"commit": {"committer": {"date": self.now}}},
        ])
        for name in ("repo-a", "repo-b"):
            api = "https://api.github.com/repos/example/" + name
            self.adapter.respond(api, {"default_branch": "builds", "pushed_at": self.now})
            self.adapter.respond(api + "/branches", [{"name": "builds"}])
            self.adapter.respond(api + "/commits", [{"commit": {"committer": {"date": self.now}}}])
            self.adapter.respond("https://raw.githubusercontent.com/example/" + name + "/builds/repo.json", {
                "name": name, "pluginLists": [SHARED_URL],
            })

    def run_scan(self, verified=None):
        if verified is None:
            verified = {"ok": True, "reason": "ok", "size": 123, "actual_hash": "sha256-" + "1" * 64,
                        "provider_check": "reachable"}
        session_factory = requests.Session

        def worker_session():
            session = session_factory()
            session.trust_env = False
            session.mount("https://", self.adapter)
            session.mount("http://", self.adapter)
            return session

        with patch.object(self.scanner.requests, "Session", side_effect=worker_session), \
             patch.object(self.scanner, "discover_candidates", return_value={
            "example/repo-a": {"seed"}, "example/repo-b": {"registry"},
        }), patch.object(self.scanner, "readme_short_addresses", return_value=[]), \
             patch.object(self.scanner, "validate_plugin_file", return_value=verified), \
             contextlib.redirect_stdout(io.StringIO()):
            self.scanner.main()

    def list_requests(self):
        return [r for r in self.adapter.requests
                if urlparse(r.url).netloc != "api.github.com" and urlparse(r.url).path.endswith("plugins.json")]

    def test_two_repositories_and_bundle_download_common_list_once(self):
        self.run_scan()

        self.assertEqual(len(self.list_requests()), 1)
        updates = [r for r in self.adapter.requests
                   if r.url.startswith("https://api.github.com/repos/source/shared/commits")]
        self.assertEqual(len(updates), 1)
        repos = json.loads((self.scanner.DATA / "repos.json").read_text(encoding="utf-8"))
        plugins = json.loads((self.scanner.ROOT / "plugins-x92.json").read_text(encoding="utf-8"))
        health = json.loads((self.scanner.DATA / "plugin-health.json").read_text(encoding="utf-8"))
        self.assertEqual(len(repos), 2)
        self.assertEqual([r["plugin_count"] for r in repos], [1, 1])
        self.assertEqual(len(plugins), 1)
        self.assertEqual(health["plugins"][0]["repository"], "example/repo-a")

    def test_raw_github_address_aliases_and_cache_busters_share_one_download(self):
        first = SHARED_URL + "?v=111"
        second = "https://raw.githubusercontent.com/source/shared/refs/heads/builds/plugins.json?t=222"
        self.adapter.respond(second.split("?", 1)[0], [self.plugin])
        for name, url in (("repo-a", first), ("repo-b", second)):
            self.adapter.respond("https://raw.githubusercontent.com/example/" + name + "/builds/repo.json", {
                "name": name, "pluginLists": [url],
            })

        self.run_scan()

        self.assertEqual(len(self.list_requests()), 1)
        repos = json.loads((self.scanner.DATA / "repos.json").read_text(encoding="utf-8"))
        self.assertEqual({r["plugin_lists"][0]["url"] for r in repos}, {first, second})

    def test_next_scan_downloads_updated_list_again(self):
        self.run_scan()
        self.adapter.respond(SHARED_URL, [dict(self.plugin, version=2)])

        self.run_scan()

        self.assertEqual(len(self.list_requests()), 2)
        plugins = json.loads((self.scanner.ROOT / "plugins-x92.json").read_text(encoding="utf-8"))
        self.assertEqual(plugins[0]["version"], 2)

    def test_failed_scan_does_not_keep_cached_lists_for_next_scan(self):
        with self.assertRaises(RuntimeError):
            self.run_scan({"ok": False, "reason": "request-error"})
        self.adapter.respond(SHARED_URL, [dict(self.plugin, version=2)])

        self.run_scan()

        self.assertEqual(len(self.list_requests()), 2)
        plugins = json.loads((self.scanner.ROOT / "plugins-x92.json").read_text(encoding="utf-8"))
        self.assertEqual(plugins[0]["version"], 2)

    def test_download_errors_are_not_cached_as_success(self):
        with self.scanner.shared_plugin_lists():
            self.adapter.respond(SHARED_URL, {}, status=503)
            with self.assertRaises(requests.HTTPError):
                self.scanner.fetch_plugin_entries(SHARED_URL)
            self.adapter.respond(SHARED_URL, [self.plugin])
            self.assertEqual(self.scanner.fetch_plugin_entries(SHARED_URL), [self.plugin])
            self.assertEqual(self.scanner.plugin_list_info(SHARED_URL)["count"], 1)
        self.assertEqual(len(self.list_requests()), 2)

    def test_missing_list_is_not_cached_as_a_valid_empty_list(self):
        with self.scanner.shared_plugin_lists():
            self.adapter.respond(SHARED_URL, {}, status=404)
            self.assertEqual(self.scanner.fetch_plugin_entries(SHARED_URL), [])
            self.adapter.respond(SHARED_URL, [self.plugin])
            self.assertEqual(self.scanner.fetch_plugin_entries(SHARED_URL), [self.plugin])
            self.assertEqual(self.scanner.plugin_list_info(SHARED_URL)["count"], 1)
        self.assertEqual(len(self.list_requests()), 2)

    def test_other_hosts_query_parameters_and_file_case_stay_distinct(self):
        urls = (
            "https://example.invalid/plugins.json?language=tr",
            "https://example.invalid/plugins.json?language=en",
            "https://cdn.jsdelivr.net/gh/source/shared@builds/plugins.json",
            SHARED_URL,
            "https://raw.githubusercontent.com/source/shared/main/plugins.json",
            "https://raw.githubusercontent.com/source/shared/builds/Plugins.json",
            SHARED_URL + "?format=other",
        )
        with self.scanner.shared_plugin_lists():
            for index, url in enumerate(urls):
                self.adapter.respond(url.split("?", 1)[0], [dict(self.plugin, version=index)])
                self.assertEqual(self.scanner.fetch_plugin_entries(url)[0]["version"], index)
        self.assertEqual(len(self.adapter.requests), len(urls))


if __name__ == "__main__":
    unittest.main()
