import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scanner" / "bridge_health.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bridge_health_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BridgeHealthTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.module.HEALTH_PATH = root / "data" / "health.json"
        self.module.PLUGINS_PATH = root / "plugins-x92.json"
        self.module.LIVE_PLUGINS_PATH = root / "data" / "live-plugins.json"
        self.module.SUMMARY_PATH = root / "data" / "bridge-health.json"
        self.root = root

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def stamp(self, days_ago=0):
        moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def test_merges_status_into_matching_plugins(self):
        alfa = {"version": 1, "url": "https://example.org/alfa.cs3"}
        beta = {"version": 2, "url": "https://example.org/beta.cs3"}
        self.write(self.module.PLUGINS_PATH, [
            {"internalName": "Alfa", "name": "Alfa", **alfa},
            {"internalName": "Beta", "name": "Beta", **beta},
        ])
        self.write(self.module.HEALTH_PATH, {
            "generated_at": self.stamp(),
            "total": 2, "done": 2, "ok": 1, "empty": 0, "dead": 1,
            "plugins": [
                {"internalName": "alfa", "status": "ok", "links": 3, "ms": 1200, **alfa},
                {"internalName": "BETA", "status": "dead", "links": 0, "ms": 800, **beta},
            ],
        })

        self.assertEqual(self.module.main(), 0)

        plugins = json.loads(self.module.PLUGINS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(plugins[0]["bridge"]["status"], "ok")
        self.assertEqual(plugins[0]["bridge"]["links"], 3)
        self.assertEqual(plugins[1]["bridge"]["status"], "dead")
        summary = json.loads(self.module.SUMMARY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(summary["matched"], 2)

    def test_leaves_plugins_without_health_untouched(self):
        alfa = {"internalName": "Alfa", "version": 1, "url": "https://example.org/alfa.cs3"}
        self.write(self.module.PLUGINS_PATH, [alfa, {"internalName": "Gama"}])
        self.write(self.module.HEALTH_PATH, {
            "generated_at": self.stamp(),
            "total": 1, "done": 1, "ok": 1, "empty": 0, "dead": 0,
            "plugins": [{**alfa, "status": "ok", "links": 1, "ms": 100}],
        })

        self.module.main()

        plugins = json.loads(self.module.PLUGINS_PATH.read_text(encoding="utf-8"))
        self.assertIn("bridge", plugins[0])
        self.assertNotIn("bridge", plugins[1])

    def test_prefers_sha256_when_both_hashes_are_available(self):
        plugin = {
            "internalName": "Alfa", "version": 2, "url": "https://example.org/alfa.cs3",
            "fileHash": "sha256-" + "a" * 64,
        }
        row = {"internalName": "Alfa", "status": "ok"}
        cases = [
            ({**row, "version": 1, "url": "https://example.org/mirror.cs3", "hash": "SHA256:" + "A" * 64}, 1),
            ({**row, "version": 2, "url": plugin["url"], "hash": "b" * 64}, 0),
            ({**row, "version": 2, "url": plugin["url"], "hash": "not-a-sha256"}, 0),
        ]
        for health_row, expected in cases:
            with self.subTest(health_row=health_row):
                entries = [dict(plugin)]
                health = {"generated_at": self.stamp(), "plugins": [health_row]}
                self.assertEqual(self.module.merge(health, entries), expected)
                self.assertEqual("bridge" in entries[0], bool(expected))

    def test_requires_version_and_url_when_report_has_no_hash(self):
        plugin = {
            "internalName": "Alfa", "version": 2, "url": "https://example.org/alfa.cs3",
            "fileHash": "sha256-" + "a" * 64,
        }
        row = {"internalName": "Alfa", "status": "ok"}
        cases = [
            ({**row, "version": 2, "url": plugin["url"]}, 1),
            ({**row, "version": 1, "url": plugin["url"]}, 0),
            ({**row, "version": 2, "url": "https://example.org/mirror.cs3"}, 0),
            ({**row, "version": 2}, 0),
            ({**row, "url": plugin["url"]}, 0),
        ]
        for health_row, expected in cases:
            with self.subTest(health_row=health_row):
                entries = [dict(plugin)]
                health = {"generated_at": self.stamp(), "plugins": [health_row]}
                self.assertEqual(self.module.merge(health, entries), expected)
                self.assertEqual("bridge" in entries[0], bool(expected))

    def test_live_list_uses_bridge_status_from_main_list(self):
        live = {"internalName": "Live", "version": 1, "url": "https://example.org/live.cs3", "tvTypes": ["Live"]}
        other_live = {"internalName": "OtherLive", "tvTypes": ["Live"]}
        movie = {"internalName": "Movie", "tvTypes": ["Movie"]}
        self.write(self.module.PLUGINS_PATH, [live, other_live, movie])
        self.write(self.module.LIVE_PLUGINS_PATH, [live, other_live])
        self.write(self.module.HEALTH_PATH, {
            "generated_at": self.stamp(),
            "plugins": [{**live, "status": "ok"}],
        })

        self.assertEqual(self.module.main(), 0)

        plugins = json.loads(self.module.PLUGINS_PATH.read_text(encoding="utf-8"))
        live_plugins = json.loads(self.module.LIVE_PLUGINS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(plugins[0]["bridge"]["status"], "ok")
        self.assertEqual(live_plugins, [plugins[0], other_live])

    def test_skips_stale_health(self):
        self.write(self.module.PLUGINS_PATH, [{"internalName": "Alfa"}])
        self.write(self.module.HEALTH_PATH, {
            "generated_at": self.stamp(days_ago=self.module.MAX_AGE_DAYS + 1),
            "total": 1, "done": 1, "ok": 1, "empty": 0, "dead": 0,
            "plugins": [{"internalName": "Alfa", "status": "ok", "links": 1, "ms": 100}],
        })

        self.assertEqual(self.module.main(), 0)

        plugins = json.loads(self.module.PLUGINS_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("bridge", plugins[0])

    def test_skips_when_health_missing(self):
        self.write(self.module.PLUGINS_PATH, [{"internalName": "Alfa"}])
        self.assertEqual(self.module.main(), 0)
        plugins = json.loads(self.module.PLUGINS_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("bridge", plugins[0])


if __name__ == "__main__":
    unittest.main()
