import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from scanner import (
    discover,
    plugin_bundle,
    plugin_validation,
    provider_addresses,
    provider_checks,
    repository_discovery,
    repository_inspection,
    scan_utils,
)
from scanner import catalog as repo_catalog
from test_discover import use_scan_root
from test_plugin_validation import DownloadResponse, package



class ScanSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        use_scan_root(self, self.root)
        scan_utils.DATA.mkdir()
        self.plugin = {"name": "Example", "internalName": "Example", "language": "tr", "status": 1,
                       "version": 1, "url": "https://example.invalid/Example.cs3"}
        self.repo = {"repository": "example/repo", "name": "Example", "status": "active", "plugin_count": 1,
                     "repo_url": "https://example.invalid/repo.json", "last_plugins_update": "2026-10-30T00:00:00Z",
                     "plugin_lists": [{"url": "https://example.invalid/plugins.json", "count": 1,
                                       "reachable": True, "languages": {"tr": 1}, "turkish_language_count": 1}]}
        scan_utils.save_json(scan_utils.DATA / "seeds.json", [{"repository": "example/repo"}])
        scan_utils.save_json(scan_utils.DATA / "repos.json", [self.repo])
        scan_utils.save_json(self.root / "plugins-x92.json", [self.plugin])
        (self.root / "README.md").write_text("# Existing repository\n", encoding="utf-8")
        self.before = self.snapshot()
        # An incomplete mock must fail rather than accidentally use the network.
        network = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_metadata_outage_preserves_existing_files(self):
        with patch.object(discover, "discover_candidates", return_value={"example/repo": {"seed"}}), \
             patch.object(repository_discovery, "api_get", side_effect=requests.ConnectionError("test outage")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                discover.main()
        self.assertEqual(self.snapshot(), self.before)

    def test_partial_scan_failure_preserves_existing_files(self):
        def inspect(name, sources):
            if name == "example/repo":
                return dict(self.repo)
            raise requests.Timeout("test outage")

        with patch.object(discover, "discover_candidates", return_value={"example/repo": {"seed"}, "other/repo": {"registry"}}), \
             patch.object(repository_inspection, "inspect_repo", side_effect=inspect), \
             patch.object(repository_discovery, "readme_short_addresses", return_value=[]), \
             patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.plugin]), \
             patch.object(plugin_validation, "validate_plugin_file", return_value={"ok": True, "reason": "ok", "provider_check": "reachable"}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                discover.main()
        self.assertEqual(self.snapshot(), self.before)

    def test_zero_verified_repositories_does_not_publish_empty_lists(self):
        with patch.object(discover, "discover_candidates", return_value={}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                discover.main()
        self.assertEqual(self.snapshot(), self.before)

    def test_manifest_outage_is_not_reported_as_missing_repository(self):
        with patch.object(repository_discovery, "api_get", return_value={"default_branch": "main"}), \
             patch.object(repository_discovery, "branch_names", return_value=["builds", "main"]), \
             patch.object(repository_discovery, "validate_manifest", side_effect=requests.Timeout("test outage")):
            with self.assertRaises(requests.RequestException):
                repository_inspection.inspect_repo("example/repo", {"seed"})

    def test_plugin_list_outage_is_not_reported_as_broken_repository(self):
        with patch.object(repository_discovery, "api_get", return_value={"default_branch": "main"}), \
             patch.object(repository_discovery, "branch_names", return_value=["builds"]), \
             patch.object(repository_discovery, "validate_manifest", return_value=(self.repo["repo_url"], {"name": "Example", "pluginLists": ["https://example.invalid/plugins.json"]})), \
             patch.object(repository_discovery, "plugin_list_info", side_effect=requests.Timeout("test outage")), \
             patch.object(repository_discovery, "latest_commit_for_path", return_value=None):
            with self.assertRaises(requests.RequestException):
                repository_inspection.inspect_repo("example/repo", {"seed"})

    def test_bundle_list_outage_preserves_existing_files(self):
        with patch.object(repository_discovery, "fetch_plugin_entries", side_effect=requests.Timeout("test outage")):
            with self.assertRaises((RuntimeError, requests.RequestException)):
                plugin_bundle.build_cloudstream_bundle([self.repo], {"example/repo"})
        self.assertEqual(self.snapshot(), self.before)

    def test_forbidden_external_list_is_reported_as_access_blocked(self):
        response = requests.Response()
        response.status_code = 403
        error = requests.HTTPError("Forbidden", response=response)
        with patch.object(repository_discovery, "api_get", return_value={"default_branch": "main"}), \
             patch.object(repository_discovery, "branch_names", return_value=["builds"]), \
             patch.object(repository_discovery, "validate_manifest", return_value=(self.repo["repo_url"], {"name": "Example", "pluginLists": ["https://example.invalid/plugins.json"]})), \
             patch.object(repository_discovery, "plugin_list_info", side_effect=error), \
             patch.object(repository_discovery, "latest_commit_for_path", return_value=None):
            result = repository_inspection.inspect_repo("example/repo", {"registry"})
        self.assertEqual(result["status"], "access-blocked")
        self.assertEqual(result["plugin_lists"][0]["check_error"], "http-403")

    def test_access_denial_for_previously_active_repo_preserves_files(self):
        blocked = dict(self.repo, status="access-blocked", plugin_count=0)
        other = dict(self.repo, repository="example/other")
        with patch.object(discover, "discover_candidates", return_value={"example/repo": {"seed"}, "example/other": {"registry"}}), \
             patch.object(repository_inspection, "inspect_repo", side_effect=lambda name, sources: blocked if name == "example/repo" else other), \
             patch.object(repository_discovery, "readme_short_addresses", return_value=[]), \
             patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.plugin]), \
             patch.object(plugin_validation, "validate_plugin_file", return_value={"ok": True, "reason": "ok", "provider_check": "reachable"}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                discover.main()
        self.assertEqual(self.snapshot(), self.before)

    def test_unrelated_blocked_repo_does_not_prevent_verified_updates(self):
        unrelated = dict(self.repo, repository="other/repo", name="Global", status="access-blocked", plugin_count=0,
                         plugin_lists=[{"url": "https://example.invalid/unavailable.json", "count": 0,
                                        "reachable": False, "check_error": "http-403"}])
        with patch.object(discover, "discover_candidates", return_value={"example/repo": {"seed"}, "other/repo": {"registry"}}), \
             patch.object(repository_inspection, "inspect_repo", side_effect=lambda name, sources: dict(self.repo) if name == "example/repo" else unrelated), \
             patch.object(repository_discovery, "readme_short_addresses", return_value=[]), \
             patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.plugin]), \
             patch.object(plugin_validation, "validate_plugin_file", return_value={"ok": True, "reason": "ok", "provider_check": "reachable"}), \
             contextlib.redirect_stdout(io.StringIO()):
            discover.main()
        global_repos = json.loads((scan_utils.DATA / "global.json").read_text(encoding="utf-8"))
        self.assertEqual(global_repos[0]["status"], "access-blocked")
        self.assertEqual(global_repos[0]["plugin_lists"][0]["check_error"], "http-403")

    def test_changed_or_invalid_plugin_list_preserves_existing_files(self):
        with patch.object(repository_discovery, "fetch_plugin_entries", return_value=[]):
            with self.assertRaises(RuntimeError):
                plugin_bundle.build_cloudstream_bundle([self.repo], {"example/repo"})
        self.assertEqual(self.snapshot(), self.before)

    def test_temporary_plugin_download_errors_preserve_existing_files(self):
        for reason in ("request-error", "http-429", "http-503"):
            with self.subTest(reason=reason), \
                 patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.plugin]), \
                 patch.object(plugin_validation, "validate_plugin_file", return_value={"ok": False, "reason": reason}):
                with self.assertRaises(RuntimeError):
                    plugin_bundle.build_cloudstream_bundle([self.repo], {"example/repo"})
                self.assertEqual(self.snapshot(), self.before)

    def test_successful_scan_updates_consistent_outputs(self):
        verified = {"ok": True, "reason": "ok", "size": 123, "actual_hash": "sha256-" + "1" * 64,
                    "provider_check": "reachable"}
        with patch.object(discover, "discover_candidates", return_value={"example/repo": {"seed"}}), \
             patch.object(repository_inspection, "inspect_repo", return_value=dict(self.repo)), \
             patch.object(repository_discovery, "readme_short_addresses", return_value=[]), \
             patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.plugin]), \
             patch.object(plugin_validation, "validate_plugin_file", return_value=verified), \
             contextlib.redirect_stdout(io.StringIO()):
            discover.main()
        plugins = json.loads((self.root / "plugins-x92.json").read_text(encoding="utf-8"))
        changes = json.loads((scan_utils.DATA / "changes.json").read_text(encoding="utf-8"))
        catalog = json.loads((scan_utils.DATA / "catalog.json").read_text(encoding="utf-8"))
        manifest = json.loads((self.root / "repo-x92.json").read_text(encoding="utf-8"))
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0]["fileSize"], 123)
        self.assertEqual(changes["app_ready_count"], 1)
        self.assertEqual(changes["published_plugin_count"], 1)
        self.assertEqual(catalog["sections"][0]["items"][0]["plugin_count"], len(plugins))
        # Clients show how long ago the list was scanned, so the manifest must carry a real timestamp.
        self.assertGreater(scan_utils.parse_iso(manifest["generated_at"]).year, 1)

    def test_verified_older_variant_is_still_used_for_bad_hash(self):
        newer = dict(self.plugin, version=2, url="https://example.invalid/new.cs3", fileHash="0" * 64)
        with patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.plugin, newer]), \
             patch.object(requests, "get", return_value=DownloadResponse(package())), \
             patch.object(provider_addresses, "extract_provider_urls", return_value=(["https://provider.invalid"], False)), \
             patch.object(provider_checks, "is_provider_dead", return_value=(False, "provider.invalid:alive")):
            self.assertEqual(plugin_bundle.build_cloudstream_bundle([self.repo], {"example/repo"}), (1, 0, 1))
        plugins = json.loads((self.root / "plugins-x92.json").read_text(encoding="utf-8"))
        health = json.loads((scan_utils.DATA / "plugin-health.json").read_text(encoding="utf-8"))
        self.assertEqual(plugins[0]["version"], 1)
        self.assertEqual(health["rejection_attempt_counts"], {"hash-mismatch": 1})
        self.assertEqual(health["provider_unverified_count"], 0)
        self.assertFalse(health["checks"]["provider_checks_are_advisory"])
        self.assertEqual(health["plugins"][0]["provider_check"], "reachable")

    def test_all_rejected_plugins_do_not_clear_existing_bundle(self):
        with patch.object(repository_discovery, "fetch_plugin_entries", return_value=[dict(self.plugin, status=0)]):
            with self.assertRaises(RuntimeError):
                plugin_bundle.build_cloudstream_bundle([self.repo], {"example/repo"})
        self.assertEqual(self.snapshot(), self.before)

    def test_catalog_bundle_targets_populated_manifest(self):
        repo_catalog.build_repo_catalog([self.repo], 1, {"example/repo"})
        catalog = json.loads((scan_utils.DATA / "catalog.json").read_text(encoding="utf-8"))
        bundle = catalog["sections"][0]["items"][0]
        self.assertTrue(bundle["repo_url"].endswith("/repo-x92.json"))
        self.assertEqual(bundle["install_url"], repo_catalog.cloudstream_install_url(bundle["repo_url"]))

    def test_stale_repositories_are_reported_without_entering_published_feed(self):
        old = {"repository": "example/repo", "status": "stale", "last_repo_push": "2020-01-01T00:00:00Z"}
        fresh = dict(self.repo, repository="example/fresh")
        verified = {"ok": True, "reason": "ok", "size": 123, "actual_hash": "sha256-" + "1" * 64,
                    "provider_check": "reachable"}
        with patch.object(discover, "discover_candidates", return_value={"example/repo": {"registry"}, "example/fresh": {"seed"}}), \
             patch.object(repository_inspection, "inspect_repo", side_effect=lambda name, sources: old if name == "example/repo" else fresh), \
             patch.object(repository_discovery, "readme_short_addresses", return_value=[]) as readme, \
             patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.plugin]) as entries, \
             patch.object(plugin_validation, "validate_plugin_file", return_value=verified), \
             contextlib.redirect_stdout(io.StringIO()):
            discover.main()

        repos = json.loads((scan_utils.DATA / "repos.json").read_text(encoding="utf-8"))
        changes = json.loads((scan_utils.DATA / "changes.json").read_text(encoding="utf-8"))
        registry = json.loads((scan_utils.DATA / "candidates.json").read_text(encoding="utf-8"))
        self.assertEqual([r["repository"] for r in repos], ["example/fresh"])
        self.assertEqual(changes["stale_repo_count"], 1)
        self.assertEqual(changes["stale_repositories"], [old])
        self.assertIn("example/repo", registry)
        self.assertEqual(readme.call_count, 1)
        self.assertEqual(readme.call_args.args[0], "example/fresh")
        entries.assert_called_once()

    def test_every_skipped_repository_records_one_fixed_reason(self):
        gone = repository_inspection.skip_repo("example/gone", "repo-not-found", "github-http-404")
        empty = repository_inspection.skip_repo("example/empty", "no-repo-json", "manifest-not-found")
        verified = {"ok": True, "reason": "ok", "size": 123, "actual_hash": "sha256-" + "1" * 64,
                    "provider_check": "reachable"}
        with patch.object(discover, "discover_candidates",
                          return_value={"example/repo": {"seed"}, "example/gone": {"registry"},
                                       "example/empty": {"registry"}}), \
             patch.object(repository_inspection, "inspect_repo",
                          side_effect=lambda name, sources: {"example/gone": gone,
                                                           "example/empty": empty}.get(name, dict(self.repo))), \
             patch.object(repository_discovery, "readme_short_addresses", return_value=[]), \
             patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.plugin]), \
             patch.object(plugin_validation, "validate_plugin_file", return_value=verified), \
             contextlib.redirect_stdout(io.StringIO()):
            discover.main()

        changes = json.loads((scan_utils.DATA / "changes.json").read_text(encoding="utf-8"))
        rows = {row["repository"]: row for row in changes["skipped_repositories"]}
        self.assertEqual(changes["skipped_repo_count"], 2)
        self.assertEqual(rows["example/gone"]["skip_reason"], "repo-not-found")
        self.assertEqual(rows["example/empty"]["skip_reason"], "no-repo-json")
        self.assertEqual(rows["example/gone"]["discovered_by"], ["registry"])
        for row in rows.values():
            self.assertTrue(row["checked_at"])

    def test_all_stale_repositories_do_not_overwrite_existing_lists(self):
        stale = {"repository": "example/repo", "status": "stale", "last_repo_push": "2020-01-01T00:00:00Z"}
        with patch.object(discover, "discover_candidates", return_value={"example/repo": {"registry"}}), \
             patch.object(repository_inspection, "inspect_repo", return_value=stale), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                discover.main()
        self.assertEqual(self.snapshot(), self.before)


if __name__ == "__main__":
    unittest.main()
