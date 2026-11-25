import contextlib
import io
import time
import unittest
from unittest.mock import patch

from scanner import scan_utils



class ScanTimingTests(unittest.TestCase):
    def test_phase_prints_measured_elapsed_time(self):
        output = io.StringIO()
        with patch.object(time, "perf_counter", side_effect=[10, 12.5]), \
             contextlib.redirect_stdout(output):
            with scan_utils.timed_phase("example"):
                pass
        self.assertIn("[time] example: 2.5 sn", output.getvalue())
        self.assertNotIn("tamamlanamadı", output.getvalue())

    def test_failed_phase_reports_time_and_preserves_original_error(self):
        output = io.StringIO()
        failure = RuntimeError("Controlled failure")
        with patch.object(time, "perf_counter", side_effect=[20, 24]), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(RuntimeError) as caught:
                with scan_utils.timed_phase("example"):
                    raise failure
        self.assertIs(caught.exception, failure)
        self.assertIn("[time] example: 4.0 sn (tamamlanamadı)", output.getvalue())


if __name__ == "__main__":
    unittest.main()
