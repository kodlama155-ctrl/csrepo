"""Check that separate Python processes resume using only saved stage files."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from test_plugin_validation import package
from test_provider_addresses import declared_url_dex


SCANNER = Path(__file__).resolve().parents[1] / "scanner/discover.py"


class StageProcessTests(unittest.TestCase):
    def test_separate_commands_reuse_state_and_real_downloads_publish_only_at_four(self):
        received = []
        responses = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                content = responses.get(self.path)
                self.send_response(200 if content is not None else 404)
                self.end_headers()
                self.wfile.write(content or b"Missing")

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
            self.addCleanup(server.server_close)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 3)
            self.addCleanup(server.shutdown)
            # This address stays on loopback and is not an ignored placeholder URL.
            base = f"http://127.0.0.2:{server.server_port}"
            archive = package(dex=declared_url_dex(base + "/provider"))
            plugin = {"name": "Example", "internalName": "Example", "language": "tr", "status": 1,
                      "url": base + "/Example.cs3", "version": 1,
                      "fileHash": "sha256-" + hashlib.sha256(archive).hexdigest()}
            responses.update({"/plugins.json": json.dumps([plugin]).encode(),
                              "/Example.cs3": archive, "/provider": b"Available"})
            data = root / "data"
            data.mkdir()
            (data / "seeds.json").write_text(json.dumps([{"repository": "example/repo"}]))
            (root / "README.md").write_text("# Preserved documentation\n", encoding="utf-8")
            (root / "plugins-x92.json").write_text("[]")
            bootstrap = root / "run_step.py"
            bootstrap.write_text(
                "import importlib.util, sys\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str(SCANNER.parent.parent)!r})\n"
                f"spec=importlib.util.spec_from_file_location('stage_process', {str(SCANNER)!r})\n"
                "s=importlib.util.module_from_spec(spec); spec.loader.exec_module(s)\n"
                "s.ROOT=Path(__file__).parent; s.DATA=s.ROOT/'data'\n"
                "def forbidden(*args, **kwargs): raise AssertionError('Earlier stage was repeated')\n"
                "s.discover_candidates=(lambda **kwargs: {'example/repo': {'seed'}}) if sys.argv[1]=='1' else forbidden\n"
                f"s.inspect_repo=(lambda *args: {{'repository':'example/repo', 'name':'Example', 'status':'active',"
                f"'plugin_count':1, 'repo_url':{(base + '/repo.json')!r}, 'plugin_lists':[{{'url':{(base + '/plugins.json')!r},"
                "'count':1,'reachable':True,'languages':{'tr':1},'turkish_language_count':1}]}) "
                "if sys.argv[1]=='2' else forbidden\n"
                "s.readme_short_addresses=lambda *args: []\n"
                "s.query_ttnet_dns=lambda *args: []\n"
                "s.cli(['--stage',sys.argv[1]])\n",
                encoding="utf-8",
            )
            env = dict(os.environ, GITHUB_TOKEN="", PYTHONIOENCODING="utf-8", NO_PROXY="*")
            output = []
            for stage in range(1, 5):
                result = subprocess.run([sys.executable, str(bootstrap), str(stage)], capture_output=True,
                                        text=True, encoding="utf-8", env=env, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                output.append(result.stdout)
                if stage < 4:
                    self.assertEqual((root / "plugins-x92.json").read_text(), "[]")
                    self.assertEqual((root / "README.md").read_text(), "# Preserved documentation\n")
            published = json.loads((root / "plugins-x92.json").read_text(encoding="utf-8"))
            self.assertEqual([item["internalName"] for item in published], ["Example"])
            self.assertEqual(published[0]["fileHash"], plugin["fileHash"])
            self.assertEqual(received, ["/plugins.json", "/Example.cs3", "/provider"])
            self.assertNotIn("[done]", "".join(output[:3]))
            self.assertIn("[done]", output[3])
            self.assertTrue((root / "README.md").read_text(encoding="utf-8").startswith("# Preserved documentation"))


if __name__ == "__main__":
    unittest.main()
