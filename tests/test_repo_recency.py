import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scanner import repository_discovery, repository_inspection
from test_discover import RecordingAdapter, use_recording_session


API_URL = "https://api.github.com/repos/example/repo"
RAW_URL = "https://raw.githubusercontent.com/example/repo/builds"


class RepositoryRecencyTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RecordingAdapter()
        use_recording_session(self, self.adapter)
        self.now = datetime.now(timezone.utc)
        self.old = (self.now - timedelta(days=366)).isoformat()
        self.recent = (self.now - timedelta(days=2)).isoformat()
        self.metadata = {"default_branch": "builds", "pushed_at": self.recent}
        self.adapter.respond(API_URL, self.metadata)
        self.adapter.respond(API_URL + "/branches", [{"name": "builds"}])
        self.adapter.respond(API_URL + "/commits", [{"commit": {"committer": {"date": self.recent}}}])
        self.adapter.respond(RAW_URL + "/repo.json", {
            "name": "Example", "pluginLists": [RAW_URL + "/plugins.json"],
        })
        self.adapter.respond(RAW_URL + "/plugins.json", [
            {"name": "Example", "internalName": "Example", "language": "tr", "status": 1},
        ])

    def test_old_repository_stops_after_metadata_without_downloading_files(self):
        self.adapter.respond(API_URL, dict(self.metadata, pushed_at=self.old))

        result = repository_inspection.inspect_repo("example/repo", {"registry"})

        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["last_repo_push"], self.old)
        self.assertEqual([r.url for r in self.adapter.requests], [API_URL])

    def test_new_push_reactivates_previously_skipped_repository(self):
        self.adapter.respond(API_URL, dict(self.metadata, pushed_at=self.old))
        self.assertEqual(repository_inspection.inspect_repo("example/repo", {"registry"})["status"], "stale")
        self.adapter.respond(API_URL, self.metadata)

        result = repository_inspection.inspect_repo("example/repo", {"registry"})

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["plugin_count"], 1)
        self.assertEqual(result["last_plugins_update"], self.recent)

    def test_recent_repo_with_old_plugin_list_skips_plugin_download(self):
        self.adapter.respond(API_URL + "/commits", [{"commit": {"committer": {"date": self.old}}}])

        result = repository_inspection.inspect_repo("example/repo", {"registry"})

        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["last_plugins_update"], self.old)
        self.assertNotIn(RAW_URL + "/plugins.json", [r.url for r in self.adapter.requests])

    def test_unknown_plugin_list_date_does_not_exclude_repository(self):
        self.adapter.respond(API_URL + "/commits", {"message": "Not Found"}, status=404)

        result = repository_inspection.inspect_repo("example/repo", {"registry"})

        self.assertEqual(result["status"], "active")
        self.assertIsNone(result["last_plugins_update"])

    def test_plugin_list_update_reactivates_repo_without_new_root_push(self):
        self.adapter.respond(API_URL + "/commits", [{"commit": {"committer": {"date": self.old}}}])
        self.assertEqual(repository_inspection.inspect_repo("example/repo", {"registry"})["status"], "stale")
        self.adapter.respond(API_URL + "/commits", [{"commit": {"committer": {"date": self.recent}}}])

        self.assertEqual(repository_inspection.inspect_repo("example/repo", {"registry"})["status"], "active")
        self.assertEqual(sum(r.url == RAW_URL + "/plugins.json" for r in self.adapter.requests), 1)

    def test_recent_or_unknown_second_list_prevents_whole_repo_exclusion(self):
        second = RAW_URL + "/other-plugins.json"
        self.adapter.respond(RAW_URL + "/repo.json", {
            "name": "Example", "pluginLists": [RAW_URL + "/plugins.json", second],
        })
        self.adapter.respond(second, [{"name": "Other", "language": "tr", "status": 1}])
        for date in (self.recent, None):
            with self.subTest(second_date=date), \
                 patch.object(repository_discovery, "plugin_list_update_info", side_effect=[
                     (self.old, "example/repo"), (date, "example/repo"),
                 ]):
                result = repository_inspection.inspect_repo("example/repo", {"registry"})
            self.assertEqual(result["status"], "active")
            self.assertEqual(result["plugin_count"], 2)

    def test_plugin_list_update_is_queried_once_before_download(self):
        result = repository_inspection.inspect_repo("example/repo", {"registry"})
        self.assertEqual(result["status"], "active")
        urls = [r.url for r in self.adapter.requests]
        updates = [i for i, url in enumerate(urls) if "path=plugins.json" in url]
        self.assertEqual(len(updates), 1)
        self.assertLess(updates[0], urls.index(RAW_URL + "/plugins.json"))

    def test_unknown_push_date_does_not_exclude_repository(self):
        for value in (None, "", "invalid", "2020-01-01T00:00:00"):
            with self.subTest(value=value):
                self.adapter.respond(API_URL, dict(self.metadata, pushed_at=value))
                self.assertEqual(repository_inspection.inspect_repo("example/repo", {"registry"})["status"], "active")

    def test_one_year_cutoff_is_inclusive_and_uses_utc(self):
        cutoff = self.now - timedelta(days=365)
        self.assertTrue(repository_discovery.repo_is_stale(cutoff.isoformat(), self.now))
        self.assertFalse(repository_discovery.repo_is_stale((cutoff + timedelta(seconds=1)).isoformat(), self.now))
        offset = timezone(timedelta(hours=3))
        self.assertTrue(repository_discovery.repo_is_stale(cutoff.astimezone(offset).isoformat(), self.now))
        self.assertFalse(repository_discovery.repo_is_stale((self.now + timedelta(days=1)).isoformat(), self.now))

    def test_repository_search_ignores_old_new_candidates(self):
        result = {"items": [
            {"full_name": "example/old", "pushed_at": self.old},
            {"full_name": "example/recent", "pushed_at": self.recent},
            {"full_name": "example/unknown"},
        ]}
        with patch.object(repository_discovery, "load_json", side_effect=lambda path, default: default), \
             patch.object(repository_discovery, "SEARCH_QUERIES", ["example query"]), \
             patch.object(repository_discovery, "CODE_QUERIES", []), \
             patch.object(repository_discovery, "api_get", return_value=result):
            found = repository_discovery.discover_candidates()

        self.assertEqual(set(found), {"example/recent", "example/unknown"})

    def test_owner_search_skips_old_manifest_probes_and_keeps_registry(self):
        data = {"seeds.json": [{"repository": "example/repo"}],
                "candidates.json": ["example/old-generic"]}
        repositories = [
            {"full_name": "example/old-generic", "name": "source", "pushed_at": self.old},
            {"full_name": "example/old-unknown", "name": "source", "pushed_at": self.old},
            {"full_name": "example/old-cloudstream", "name": "cloudstream", "pushed_at": self.old},
            {"full_name": "example/recent-cloudstream", "name": "cloudstream", "pushed_at": self.recent},
        ]
        with patch.object(repository_discovery, "load_json", side_effect=lambda path, default: data.get(path.name, default)), \
             patch.object(repository_discovery, "SEARCH_QUERIES", []), \
             patch.object(repository_discovery, "CODE_QUERIES", []), \
             patch.object(repository_discovery, "api_get", return_value=repositories), \
             patch.object(repository_discovery, "text_get", side_effect=AssertionError("Old manifest downloaded")) as download:
            found = repository_discovery.discover_candidates()

        download.assert_not_called()
        self.assertEqual(set(found), {"example/repo", "example/old-generic", "example/recent-cloudstream"})


if __name__ == "__main__":
    unittest.main()
