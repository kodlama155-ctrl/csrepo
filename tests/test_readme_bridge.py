import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scanner import catalog, scan_utils
from test_discover import use_scan_root


class ReadmeBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        use_scan_root(self, Path(temporary.name))
        scan_utils.DATA.mkdir(parents=True, exist_ok=True)

    def write_health(self, minutes_ago, **counts):
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        payload = {
            "generated_at": stamp.isoformat().replace("+00:00", "Z"),
            "total": 351, "done": 300,
            "ok": 10, "alive": 2, "dead": 3, "unknown": 285,
        }
        payload.update(counts)
        scan_utils.save_json(scan_utils.DATA / "health.json", payload)

    def test_missing_health_has_no_line(self):
        self.assertIsNone(catalog.bridge_status_line())

    def test_fresh_health_shows_counts(self):
        self.write_health(90)
        line = catalog.bridge_status_line()
        self.assertIn("**Köprü:**", line)
        self.assertIn("ok 10", line)
        self.assertIn("alive 2", line)
        self.assertIn("unknown 285", line)
        self.assertIn("300/351", line)
        self.assertNotIn("UYARI", line)

    def test_stale_health_warns(self):
        self.write_health(2 * 24 * 60)
        self.assertIn("UYARI: telefon sessiz", catalog.bridge_status_line())

    def test_readme_contains_bridge_line(self):
        self.write_health(30)
        catalog.build_main_readme([], {}, "2026-11-26T00:00:00Z")
        text = (scan_utils.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("**Köprü:** son tarama", text)


if __name__ == "__main__":
    unittest.main()
