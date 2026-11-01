import contextlib
import importlib.util
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch


SCANNER_PATH = Path(__file__).resolve().parents[1] / "scanner" / "discover.py"


class ScanTimingTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("scan_timing", SCANNER_PATH)
        self.scanner = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            spec.loader.exec_module(self.scanner)
        self.addCleanup(self.scanner.SESSION.close)

    def test_phase_prints_measured_elapsed_time(self):
        output = io.StringIO()
        with patch.object(self.scanner.time, "perf_counter", side_effect=[10, 12.5]), \
             contextlib.redirect_stdout(output):
            with self.scanner.timed_phase("example"):
                pass
        self.assertIn("[time] example: 2.5 sn", output.getvalue())
        self.assertNotIn("tamamlanamadı", output.getvalue())

    def test_failed_phase_reports_time_and_preserves_original_error(self):
        output = io.StringIO()
        failure = RuntimeError("Controlled failure")
        with patch.object(self.scanner.time, "perf_counter", side_effect=[20, 24]), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(RuntimeError) as caught:
                with self.scanner.timed_phase("example"):
                    raise failure
        self.assertIs(caught.exception, failure)
        self.assertIn("[time] example: 4.0 sn (tamamlanamadı)", output.getvalue())


if __name__ == "__main__":
    unittest.main()
