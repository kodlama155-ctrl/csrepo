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
        self.module.RESULTS_PATH = root / "data" / "bridge-results.json"
        self.root = root
        self.scan_tick = 0

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
                results = self.module.update_results({}, health, entries)
                matched, _, _ = self.module.apply_results(results, entries)
                self.assertEqual(matched, expected)
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
                results = self.module.update_results({}, health, entries)
                matched, _, _ = self.module.apply_results(results, entries)
                self.assertEqual(matched, expected)
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

    def run_scan(self, status, plugins, stamp=None):
        def status_of(plugin):
            return status.get(plugin["internalName"], "unknown") if isinstance(status, dict) else status

        self.scan_tick += 1
        stamp = stamp or (datetime.now(timezone.utc) + timedelta(minutes=self.scan_tick)).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
        self.write(self.module.PLUGINS_PATH, plugins)
        self.write(self.module.HEALTH_PATH, {
            "generated_at": stamp or self.stamp(),
            "total": len(plugins), "done": len(plugins),
            "ok": sum(1 for p in plugins if status_of(p) == "ok"),
            "empty": 0,
            "dead": sum(1 for p in plugins if status_of(p) == "dead"),
            "plugins": [{**plugin, "status": status_of(plugin)} for plugin in plugins],
        })
        self.assertEqual(self.module.main(), 0)
        return json.loads(self.module.PLUGINS_PATH.read_text(encoding="utf-8"))

    def results(self):
        return json.loads(self.module.RESULTS_PATH.read_text(encoding="utf-8"))

    def test_three_consecutive_dead_scans_remove_plugin(self):
        dead = {"internalName": "Olu", "name": "Olu", "version": 1, "url": "https://example.org/olu.cs3"}
        live = {**dead, "internalName": "Sag", "name": "Sag", "url": "https://example.org/sag.cs3"}
        live["tvTypes"] = ["Live"]

        for expected_streak in (1, 2):
            plugins = self.run_scan({"Olu": "dead", "Sag": "ok"}, [dict(dead), dict(live)])
            self.assertEqual([p["internalName"] for p in plugins], ["Olu", "Sag"])
            self.assertEqual(plugins[0]["bridge"]["dead_streak"], expected_streak)

        plugins = self.run_scan({"Olu": "dead", "Sag": "ok"}, [dict(dead), dict(live)])
        self.assertEqual([p["internalName"] for p in plugins], ["Sag"])
        self.assertEqual(self.results()["plugins"][0]["dead_streak"], 3)
        summary = json.loads(self.module.SUMMARY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(summary["removed_count"], 1)
        self.assertEqual(summary["removed"][0]["internal_name"], "olu")
        live_plugins = json.loads(self.module.LIVE_PLUGINS_PATH.read_text(encoding="utf-8"))
        self.assertEqual([p["internalName"] for p in live_plugins], ["Sag"])

    def test_unknown_does_not_count_or_reset_dead_streak(self):
        plugin = {"internalName": "Olu", "name": "Olu", "version": 1, "url": "https://example.org/olu.cs3"}
        self.run_scan("dead", [dict(plugin)])
        self.run_scan("dead", [dict(plugin)])
        self.run_scan("unknown", [dict(plugin)])
        self.assertEqual(self.results()["plugins"][0]["dead_streak"], 2)

        plugins = self.run_scan("dead", [dict(plugin)])
        self.assertEqual(plugins, [])
        self.assertEqual(self.results()["plugins"][0]["dead_streak"], 3)

    def test_same_report_is_not_counted_twice(self):
        plugin = {"internalName": "Olu", "name": "Olu", "version": 1, "url": "https://example.org/olu.cs3"}
        stamp = self.stamp()
        self.run_scan("dead", [dict(plugin)], stamp=stamp)
        plugins = self.run_scan("dead", [dict(plugin)], stamp=stamp)
        self.assertEqual(plugins[0]["bridge"]["dead_streak"], 1)
        self.assertEqual(self.results()["plugins"][0]["dead_streak"], 1)

    def test_ok_resets_dead_streak(self):
        plugin = {"internalName": "Alfa", "name": "Alfa", "version": 1, "url": "https://example.org/alfa.cs3"}
        self.run_scan("dead", [dict(plugin)])
        self.run_scan("dead", [dict(plugin)])
        self.run_scan("ok", [dict(plugin)])
        plugins = self.run_scan("dead", [dict(plugin)])
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0]["bridge"]["dead_streak"], 1)

    def test_results_survive_a_scan_without_health(self):
        plugin = {"internalName": "Alfa", "name": "Alfa", "version": 1, "url": "https://example.org/alfa.cs3"}
        self.run_scan("ok", [dict(plugin)])
        self.module.HEALTH_PATH.unlink()

        self.write(self.module.PLUGINS_PATH, [dict(plugin)])
        self.assertEqual(self.module.main(), 0)

        plugins = json.loads(self.module.PLUGINS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(plugins[0]["bridge"]["status"], "ok")
        self.assertEqual(plugins[0]["bridge"]["dead_streak"], 0)

    def test_old_results_are_pruned(self):
        plugin = {"internalName": "Alfa", "name": "Alfa", "version": 1, "url": "https://example.org/alfa.cs3"}
        self.write(self.module.RESULTS_PATH, {
            "generated_at": self.stamp(days_ago=self.module.RESULTS_MAX_AGE_DAYS + 1),
            "plugins": [{
                **plugin, "internal_name": "alfa", "status": "dead", "dead_streak": 3,
                "checked_at": self.stamp(days_ago=self.module.RESULTS_MAX_AGE_DAYS + 1),
            }],
        })
        self.write(self.module.PLUGINS_PATH, [dict(plugin)])

        self.assertEqual(self.module.main(), 0)

        plugins = json.loads(self.module.PLUGINS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(plugins), 1)
        self.assertNotIn("bridge", plugins[0])
        self.assertEqual(self.results()["plugins"], [])

    def test_changed_file_does_not_inherit_dead_streak(self):
        old = {"internalName": "Alfa", "version": 1, "url": "https://example.org/alfa.cs3"}
        new = {**old, "version": 2, "url": "https://example.org/alfa-v2.cs3"}
        self.run_scan("dead", [dict(old)])
        self.run_scan("dead", [dict(old)])
        self.assertEqual(self.results()["plugins"][0]["dead_streak"], 2)

        plugins = self.run_scan("dead", [dict(new)])
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0]["bridge"]["dead_streak"], 1)


if __name__ == "__main__":
    unittest.main()
