import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from scanner import (
    catalog,
    discover,
    plugin_validation,
    repository_discovery,
    repository_inspection,
    scan_utils,
    stages,
)
from test_discover import use_scan_root



class ScanStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        use_scan_root(self, self.root)
        self.state = self.root / ".scan-state"
        self.plugin = {"name": "Example", "internalName": "Example", "language": "tr", "status": 1,
                       "version": 1, "url": "https://example.invalid/Example.cs3"}
        self.repo = {"repository": "example/repo", "name": "Example", "status": "active", "plugin_count": 1,
                     "repo_url": "https://example.invalid/repo.json",
                     "plugin_lists": [{"url": "https://example.invalid/plugins.json", "count": 1,
                                       "reachable": True, "languages": {"tr": 1}, "turkish_language_count": 1}]}
        scan_utils.save_json(scan_utils.DATA / "seeds.json", [{"repository": "example/repo"}])
        scan_utils.save_json(scan_utils.DATA / "repos.json", [self.repo])
        scan_utils.save_json(self.root / "plugins-x92.json", [{"internalName": "Previous"}])
        (self.root / "README.md").write_text("# Existing documentation\n", encoding="utf-8")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(requests.sessions.Session, "request",
                                       side_effect=AssertionError("Unexpected network request")))
        self.discover = stack.enter_context(patch.object(discover, "discover_candidates",
                                                        return_value={"example/repo": {"seed"}}))
        self.inspect = stack.enter_context(patch.object(repository_inspection, "inspect_repo",
                                                       side_effect=lambda *args: copy.deepcopy(self.repo)))
        stack.enter_context(patch.object(repository_discovery, "readme_short_addresses", return_value=[]))
        self.entries = stack.enter_context(patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.plugin]))
        self.validate = stack.enter_context(patch.object(plugin_validation, "validate_plugin_file", return_value={
            "ok": True, "reason": "ok", "size": 123, "actual_hash": "sha256-" + "1" * 64,
            "provider_check": "reachable", "provider_reason": "provider.invalid:alive",
        }))
        self.store = stages.StageStore(discover.scan_api(), self.state)
        self.before = self.outputs()

    def outputs(self):
        return {name: (self.root / name).read_bytes() if (self.root / name).exists() else None
                for name in stages.OUTPUT_FILES}

    def run_steps(self, *numbers):
        with contextlib.redirect_stdout(io.StringIO()):
            return stages.run_stages(discover.scan_api(), numbers, state_dir=self.state)

    def test_separate_steps_preserve_public_files_until_four_and_reuse_prior_results(self):
        for number in (1, 2, 3):
            self.run_steps(number)
            self.assertEqual(self.outputs(), self.before)
        self.assertTrue((self.state / "stage-3.json").exists())
        self.run_steps(4)
        published = json.loads((self.root / "plugins-x92.json").read_text())
        changes = json.loads((self.root / "data/changes.json").read_text())
        self.assertEqual([p["internalName"] for p in published], ["Example"])
        self.assertEqual(changes["published_plugin_count"], len(published))
        self.assertTrue((self.root / "README.md").read_text(encoding="utf-8").startswith("# Existing documentation"))
        self.discover.assert_called_once()
        self.inspect.assert_called_once()
        self.entries.assert_called_once()
        self.validate.assert_called()
        self.assertEqual(self.store.load(4)["stage"], 4)

    def test_stage_three_reads_published_list_for_transient_site_errors(self):
        stable = dict(self.plugin, name="Stable", internalName="Stable",
                      url="https://example.invalid/stable.cs3")
        self.entries.return_value = [self.plugin, stable]
        self.run_steps(1, 2, 3, 4)
        published_path = self.root / "plugins-x92.json"
        before = scan_utils.load_json(published_path, [])

        verified = self.validate.return_value
        temporary_failure = dict(verified, provider_check="unverified", provider_reason="provider.invalid:http-500")

        def validate(plugin, *args):
            return temporary_failure if plugin["internalName"] == "Example" else verified

        self.validate.side_effect = validate
        self.run_steps(3)
        self.assertEqual(scan_utils.load_json(published_path, []), before)
        self.run_steps(4)

        published = scan_utils.load_json(published_path, [])
        self.assertEqual([plugin["internalName"] for plugin in published], ["Example", "Stable"])
        self.assertEqual(published[0], dict(before[0], recheck=True))

    def test_continue_from_three_skips_discovery_and_inspection(self):
        self.run_steps(1, 2)
        self.discover.reset_mock()
        self.inspect.reset_mock()
        with contextlib.redirect_stdout(io.StringIO()):
            discover.cli(["--from-stage", "3", "--state-dir", str(self.state)])
        self.discover.assert_not_called()
        self.inspect.assert_not_called()
        self.assertEqual(self.store.load(4)["stage"], 4)

    def test_single_stage_cli_stops_after_requested_step(self):
        with contextlib.redirect_stdout(io.StringIO()):
            discover.cli(["--stage", "1", "--state-dir", str(self.state)])
        self.assertTrue((self.state / "stage-1.json").exists())
        self.assertFalse((self.state / "stage-2.json").exists())
        self.inspect.assert_not_called()
        self.entries.assert_not_called()
        self.assertEqual(self.outputs(), self.before)

    def test_default_cli_runs_all_steps_and_persists_them(self):
        with contextlib.redirect_stdout(io.StringIO()):
            discover.cli([])
        for stage in range(1, 5):
            self.assertEqual(self.store.load(stage)["stage"], stage)
        self.discover.assert_called_once()
        self.inspect.assert_called_once()

    def test_status_does_not_run_a_scan_or_create_state(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            discover.cli(["--status", "--state-dir", str(self.state)])
        self.assertIn("hazır değil", output.getvalue())
        self.assertFalse(self.state.exists())
        self.discover.assert_not_called()
        self.inspect.assert_not_called()
        self.entries.assert_not_called()

    def test_missing_previous_step_does_not_start_plugin_checks(self):
        with self.assertRaisesRegex(RuntimeError, "--stage 2"):
            self.run_steps(3)
        self.entries.assert_not_called()
        self.assertEqual(self.outputs(), self.before)

    def test_rerunning_inspection_invalidates_old_plugin_results(self):
        self.run_steps(1, 2, 3)
        self.run_steps(2)
        with self.assertRaisesRegex(RuntimeError, "--stage 3"):
            self.run_steps(4)
        self.assertEqual(self.outputs(), self.before)
        self.run_steps(3, 4)
        self.assertEqual(self.store.load(4)["stage"], 4)

    def test_rerunning_discovery_invalidates_later_steps(self):
        self.run_steps(1, 2, 3)
        self.run_steps(1)
        with self.assertRaisesRegex(RuntimeError, "--stage 2"):
            self.run_steps(3)
        self.assertEqual(self.outputs(), self.before)

    def test_failed_inspection_keeps_its_last_successful_checkpoint(self):
        self.run_steps(1, 2)
        previous = (self.state / "stage-2.json").read_bytes()
        self.inspect.side_effect = requests.Timeout("Controlled outage")
        with self.assertRaises(RuntimeError):
            self.run_steps(2)
        self.assertEqual((self.state / "stage-2.json").read_bytes(), previous)
        self.assertEqual(self.outputs(), self.before)

    def test_failed_plugin_download_keeps_last_successful_checkpoint(self):
        self.run_steps(1, 2, 3)
        previous = (self.state / "stage-3.json").read_bytes()
        self.validate.return_value = {"ok": False, "reason": "request-error"}
        with self.assertRaises(RuntimeError):
            self.run_steps(3)
        self.assertEqual((self.state / "stage-3.json").read_bytes(), previous)
        self.assertEqual(self.outputs(), self.before)

    def test_changed_seeds_require_discovery_again(self):
        self.run_steps(1, 2, 3)
        scan_utils.save_json(scan_utils.DATA / "seeds.json", [{"repository": "other/repo"}])
        with self.assertRaisesRegex(RuntimeError, "--stage 1"):
            self.run_steps(4)
        self.assertEqual(self.outputs(), self.before)

    def test_changed_blacklist_requires_only_plugin_checks_again(self):
        self.run_steps(1, 2, 3)
        scan_utils.save_json(scan_utils.DATA / "blacklist.json", {"names": ["Other"]})
        with self.assertRaisesRegex(RuntimeError, "--stage 3"):
            self.run_steps(4)
        self.assertEqual(self.outputs(), self.before)
        self.run_steps(3, 4)
        self.assertEqual(self.store.load(4)["stage"], 4)

    def test_sources_changed_during_discovery_do_not_create_a_checkpoint(self):
        def discover(**kwargs):
            scan_utils.save_json(scan_utils.DATA / "seeds.json", [{"repository": "other/repo"}])
            return {"example/repo": {"seed"}}

        self.discover.side_effect = discover
        with self.assertRaisesRegex(RuntimeError, "çalışma sırasında değişti"):
            self.run_steps(1)
        self.assertFalse((self.state / "stage-1.json").exists())
        self.assertEqual(self.outputs(), self.before)

    def test_blacklist_changed_during_checks_does_not_save_mixed_results(self):
        self.run_steps(1, 2)

        def entries(url):
            scan_utils.save_json(scan_utils.DATA / "blacklist.json", {"names": ["Other"]})
            return [self.plugin]

        self.entries.side_effect = entries
        with self.assertRaisesRegex(RuntimeError, "çalışma sırasında değişti"):
            self.run_steps(3)
        self.assertFalse((self.state / "stage-3.json").exists())
        self.assertEqual(self.outputs(), self.before)

    def test_modified_artifact_cannot_be_published(self):
        self.run_steps(1, 2, 3)
        record = self.store.load(3)
        artifact = self.store.artifact_path(record["payload"]["artifact_dir"])
        (artifact / "plugins-x92.json").write_text("[]")
        with self.assertRaisesRegex(RuntimeError, "--stage 3"):
            self.run_steps(4)
        self.assertEqual(self.outputs(), self.before)

    def test_catalog_failure_preserves_all_public_files(self):
        self.run_steps(1, 2, 3)
        with patch.object(catalog, "build_repo_catalog", side_effect=OSError("Controlled write failure")):
            with self.assertRaises(OSError):
                self.run_steps(4)
        self.assertEqual(self.outputs(), self.before)
        self.assertFalse((self.state / "stage-4.json").exists())

    def test_publication_failure_rolls_back_new_and_existing_files(self):
        self.run_steps(1, 2, 3)
        original = stages.atomic_bytes
        failed = False

        def write(path, content):
            nonlocal failed
            if path == self.root / "data/catalog.json" and not failed:
                failed = True
                raise OSError("Controlled publication failure")
            return original(path, content)

        with patch.object(stages, "atomic_bytes", side_effect=write):
            with self.assertRaises(OSError):
                self.run_steps(4)
        self.assertTrue(failed)
        self.assertEqual(self.outputs(), self.before)

    def test_checkpoint_failure_after_publication_rolls_back_files(self):
        self.run_steps(1, 2, 3)
        original = stages.atomic_bytes

        def write(path, content):
            if path == self.state / "stage-4.json":
                raise OSError("Controlled checkpoint failure")
            return original(path, content)

        with patch.object(stages, "atomic_bytes", side_effect=write):
            with self.assertRaises(OSError):
                self.run_steps(4)
        self.assertEqual(self.outputs(), self.before)

    def test_corrupt_checkpoint_stops_resume(self):
        self.run_steps(1, 2)
        (self.state / "stage-2.json").write_text("{broken")
        with self.assertRaisesRegex(RuntimeError, "--stage 2"):
            self.run_steps(3)
        self.entries.assert_not_called()
        self.assertEqual(self.outputs(), self.before)

    def test_another_stage_cannot_run_in_locked_workspace(self):
        with stages.work_lock(self.state):
            with self.assertRaisesRegex(RuntimeError, "başka bir tarama"):
                self.run_steps(1)
        self.discover.assert_not_called()
        self.assertEqual(self.outputs(), self.before)


if __name__ == "__main__":
    unittest.main()
