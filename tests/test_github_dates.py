import unittest
from urllib.parse import parse_qs, urlparse

from scanner import repository_discovery, repository_inspection
from test_discover import RecordingAdapter, repository_date, use_recording_session, use_repository_clock


API_URL = "https://api.github.com/repos/example/repo"
RAW_URL = "https://raw.githubusercontent.com/example/repo"
PLUGIN_DATE = repository_date(days_ago=33)
MANIFEST_DATE = repository_date(days_ago=27)
PUSH_DATE = repository_date(days_ago=25)


class CommitDateAdapter(RecordingAdapter):
    """Only return a commit when the requested branch and file exist."""

    def __init__(self):
        super().__init__()
        self.commit_dates = {"repo.json": MANIFEST_DATE, "plugins.json": PLUGIN_DATE}

    def send(self, request, **kwargs):
        if request.url.split("?", 1)[0] == API_URL + "/commits":
            query = parse_qs(urlparse(request.url).query)
            file_path = query.get("path", [""])[0]
            date = self.commit_dates.get(file_path) if query.get("sha") == ["builds"] else None
            if date:
                self.respond(API_URL + "/commits", [{"commit": {"committer": {"date": date}}}])
            else:
                self.respond(API_URL + "/commits", {"message": "Not Found"}, status=404)
        return super().send(request, **kwargs)


class GitHubUpdateDateTests(unittest.TestCase):
    def setUp(self):
        use_repository_clock(self)
        self.adapter = CommitDateAdapter()
        use_recording_session(self, self.adapter)
        self.adapter.respond(API_URL, {
            "default_branch": "builds", "html_url": "https://github.com/example/repo",
            "pushed_at": PUSH_DATE,
        })
        self.adapter.respond(API_URL + "/branches", [{"name": "builds"}])

    def inspect(self, plugin_url):
        self.adapter.respond(RAW_URL + "/builds/repo.json", {
            "name": "Example", "manifestVersion": 1, "pluginLists": [plugin_url],
        })
        self.adapter.respond(plugin_url.split("?", 1)[0], [
            {"name": "Example", "internalName": "Example", "language": "tr", "status": 1},
        ])
        return repository_inspection.inspect_repo("example/repo", {"registry"})

    def assert_plugin_date(self, result):
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["plugin_count"], 1)
        self.assertEqual(result["plugin_lists"][0]["updated_at"], PLUGIN_DATE)
        self.assertEqual(result["last_plugins_update"], PLUGIN_DATE)
        self.assertEqual(result["last_manifest_update"], MANIFEST_DATE)
        self.assertEqual(result["last_repo_push"], PUSH_DATE)

    def test_short_raw_url_preserves_plugin_update_date(self):
        result = self.inspect(RAW_URL + "/builds/plugins.json")
        self.assert_plugin_date(result)

    def test_explicit_branch_url_recovers_plugin_update_date(self):
        result = self.inspect(RAW_URL + "/refs/heads/builds/plugins.json?v=123")
        self.assert_plugin_date(result)

    def test_nested_explicit_branch_url_preserves_file_path(self):
        self.adapter.commit_dates["lists/plugins.json"] = PLUGIN_DATE
        result = self.inspect(RAW_URL + "/refs/heads/builds/lists/plugins.json")
        self.assert_plugin_date(result)

    def test_github_download_url_recovers_plugin_update_date(self):
        result = self.inspect("https://github.com/example/repo/raw/refs/heads/builds/plugins.json")
        self.assert_plugin_date(result)
        self.assertEqual(result["plugin_source_repositories"], ["example/repo"])

    def test_jsdelivr_branch_url_recovers_plugin_update_date(self):
        result = self.inspect("https://cdn.jsdelivr.net/gh/example/repo@builds/plugins.json")
        self.assert_plugin_date(result)
        self.assertEqual(result["plugin_source_repositories"], ["example/repo"])

    def test_unrecognized_addresses_do_not_invent_commit_locations(self):
        for url in (
            "https://example.invalid/example/repo/builds/plugins.json",
            "https://raw.githubusercontent.com.example.invalid/example/repo/builds/plugins.json",
            "https://github.com/example/repo/blob/builds/plugins.json",
            "https://cdn.jsdelivr.net/npm/example/plugins.json",
            "https://cdn.jsdelivr.net/gh/example/repo/plugins.json",
            "https://cdn.jsdelivr.net/gh/example/repo@/plugins.json",
        ):
            with self.subTest(url=url):
                self.assertIsNone(repository_discovery.parse_raw_github(url))

    def test_missing_commit_history_keeps_update_date_unknown(self):
        del self.adapter.commit_dates["plugins.json"]
        result = self.inspect(RAW_URL + "/builds/plugins.json")
        self.assertEqual(result["status"], "active")
        self.assertIsNone(result["plugin_lists"][0]["updated_at"])
        self.assertIsNone(result["last_plugins_update"])
        self.assertEqual(result["last_manifest_update"], MANIFEST_DATE)


if __name__ == "__main__":
    unittest.main()
