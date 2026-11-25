import json
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from scanner import repository_discovery, repository_inspection
from test_discover import RecordingAdapter


SHARED_URL = "https://raw.githubusercontent.com/source/shared/builds/plugins.json"
ORIGINAL_SESSION_REQUEST = requests.sessions.Session.request


class ParallelRepositoryTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)

    def test_four_workers_overlap_and_results_keep_repository_order(self):
        barrier = threading.Barrier(4, timeout=3)
        lock = threading.Lock()
        active, peak = 0, 0
        sessions = {}
        candidates = {f"example/repo-{index}": {"registry"} for index in reversed(range(8))}

        def inspect(name, sources):
            nonlocal active, peak
            session = repository_discovery._HTTP_SESSION.value
            with lock:
                active += 1
                peak = max(peak, active)
                sessions.setdefault(threading.get_ident(), set()).add(id(session))
            try:
                self.assertIsNot(session, repository_discovery.SESSION)
                self.assertEqual(session.headers["User-Agent"], repository_discovery.HEADERS["User-Agent"])
                barrier.wait()
                return {"repository": name, "sources": sources}
            finally:
                with lock:
                    active -= 1

        with patch.object(repository_inspection, "inspect_repo", side_effect=inspect):
            rows = list(repository_inspection.inspect_repositories(candidates))
        self.assertEqual(peak, 4)
        self.assertEqual(len(sessions), 4)
        self.assertTrue(all(len(values) == 1 for values in sessions.values()))
        self.assertEqual(len(set.union(*sessions.values())), 4)
        self.assertEqual([name for name, _, _ in rows], sorted(candidates))
        self.assertTrue(all(error is None for _, _, error in rows))
        self.assertEqual([item["repository"] for _, item, _ in rows], sorted(candidates))
        self.assertFalse(hasattr(repository_discovery._HTTP_SESSION, "value"))

    def test_worker_failure_is_returned_with_other_results_and_sessions_close(self):
        failure = requests.Timeout("Controlled failure")
        session_factory = requests.Session
        sessions = []

        def make_session():
            session = session_factory()
            original_close = session.close
            session.close = Mock(wraps=original_close)
            sessions.append(session)
            return session

        def inspect(name, sources):
            if name == "example/bad":
                raise failure
            return {"repository": name}

        with patch.object(requests, "Session", side_effect=make_session), \
             patch.object(repository_inspection, "inspect_repo", side_effect=inspect):
            rows = list(repository_inspection.inspect_repositories({"example/good": {"seed"}, "example/bad": {"registry"}}))
        self.assertIs(rows[0][2], failure)
        self.assertIsNone(rows[0][1])
        self.assertEqual(rows[1][1], {"repository": "example/good"})
        self.assertIsNone(rows[1][2])
        self.assertTrue(sessions)
        for session in sessions:
            session.close.assert_called_once()

    def test_parallel_workers_and_parent_share_each_list_and_update_once(self):
        barrier = threading.Barrier(4, timeout=3)
        plugin = {"internalName": "Example", "language": "tr", "status": 1}

        def inspect(name, sources):
            barrier.wait()
            return repository_discovery.plugin_list_info(SHARED_URL)

        with patch.object(repository_inspection, "inspect_repo", side_effect=inspect), \
             patch.object(repository_discovery, "text_get", return_value=(json.dumps([plugin]), None)) as download, \
             patch.object(repository_discovery, "latest_commit_for_path", return_value="2026-11-01T00:00:00Z") as update:
            with repository_discovery.shared_plugin_lists():
                rows = list(repository_inspection.inspect_repositories({f"example/{i}": {"seed"} for i in range(4)}))
                self.assertTrue(all(error is None for _, _, error in rows))
                self.assertEqual([item["count"] for _, item, _ in rows], [1] * 4)
                self.assertEqual(repository_discovery.fetch_plugin_entries(SHARED_URL), [plugin])
                self.assertEqual(repository_discovery.plugin_list_update_info(SHARED_URL)[0], "2026-11-01T00:00:00Z")
            self.assertIsNone(repository_discovery._PLUGIN_LIST_CACHE.get())
            download.assert_called_once()
            update.assert_called_once()

    def test_worker_connections_preserve_api_only_authorization(self):
        # Use the real request preparation path through an in-memory adapter.
        barrier = threading.Barrier(2, timeout=3)
        adapter = RecordingAdapter()
        api = "https://api.github.com/repos/example/repo"
        raw = "https://raw.githubusercontent.com/example/repo/main/repo.json"
        adapter.respond(api, {"name": "Example"})
        adapter.respond(raw, {"name": "Example"})
        factory = requests.Session

        def make_session():
            session = factory()
            session.trust_env = False
            session.mount("https://", adapter)
            return session

        def inspect(name, sources):
            barrier.wait()
            repository_discovery.api_get("/repos/example/repo")
            return repository_discovery.text_get(raw)

        # Session.request is restored for prepared requests; adapter.send never opens a socket.
        with patch.object(requests.sessions.Session, "request", ORIGINAL_SESSION_REQUEST), \
             patch.object(requests, "Session", side_effect=make_session), \
             patch.object(repository_discovery, "TOKEN", "placeholder-test-token"), \
             patch.object(repository_inspection, "inspect_repo", side_effect=inspect):
            rows = list(repository_inspection.inspect_repositories({"example/a": {"seed"}, "example/b": {"seed"}}))
        self.assertTrue(all(error is None for _, _, error in rows))
        self.assertEqual(len(adapter.requests), 4)
        for request in adapter.requests:
            if request.url == api:
                self.assertEqual(request.headers.get("Authorization"), "Bearer placeholder-test-token")
            else:
                self.assertNotIn("Authorization", request.headers)

    def test_empty_candidates_do_not_open_worker_connections(self):
        with patch.object(requests, "Session") as make_session:
            self.assertEqual(list(repository_inspection.inspect_repositories({})), [])
        make_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
