import hashlib
import importlib.util
import io
import json
import os
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch


SCANNER_PATH = Path(__file__).resolve().parents[1] / "scanner" / "discover.py"


def dex_fixture():
    data = bytearray(112)
    data[:8] = b"dex\n035\0"
    data[32:36] = len(data).to_bytes(4, "little")
    data[36:40] = (112).to_bytes(4, "little")
    data[40:44] = (0x12345678).to_bytes(4, "little")
    data[12:32] = hashlib.sha1(data[32:]).digest()
    data[8:12] = zlib.adler32(data[12:]).to_bytes(4, "little")
    return bytes(data)


def package(manifest=None, dex=None):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"pluginClassName": "example.Plugin"}) if manifest is None else manifest)
        archive.writestr("classes.dex", dex_fixture() if dex is None else dex)
    return result.getvalue()


class DownloadResponse:
    def __init__(self, content):
        self.content = content
        self.status_code = 200
        self.headers = {"Content-Length": str(len(content))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        yield self.content


class PluginValidationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("plugin_validation", SCANNER_PATH)
        self.scanner = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            spec.loader.exec_module(self.scanner)
        self.addCleanup(self.scanner.SESSION.close)
        self.plugin = {"name": "Example", "internalName": "Example", "status": 1,
                       "url": "https://example.invalid/Example.cs3"}

    def validate(self, content, **changes):
        with patch.object(self.scanner.requests, "get", return_value=DownloadResponse(content)):
            return self.scanner.validate_plugin_file(dict(self.plugin, **changes), {})

    def test_well_formed_manifest_and_dex_pass_integrity_checks(self):
        content = package()
        result = self.validate(content, fileHash="sha256-" + hashlib.sha256(content).hexdigest())
        self.assertTrue(result["ok"])
        self.assertTrue(result["hash_checked"])

    def test_invalid_manifest_is_rejected(self):
        for manifest in ("not JSON", "[]", "{}", '{"pluginClassName": " "}', '{"pluginClassName": 123}'):
            with self.subTest(manifest=manifest):
                self.assertEqual(self.validate(package(manifest=manifest))["reason"], "invalid-plugin-manifest")

    def test_invalid_dex_is_rejected_even_without_site_check(self):
        for dex in (b"not DEX", b"dex\n035\0", bytes(112)):
            with self.subTest(dex_size=len(dex)):
                self.assertEqual(self.validate(package(dex=dex))["reason"], "invalid-dex")

    def test_corrupted_dex_size_or_checksum_is_rejected(self):
        for offset in (8, 12, 32, 36, 40, 100):
            with self.subTest(offset=offset):
                dex = bytearray(dex_fixture())
                dex[offset] ^= 1
                self.assertEqual(self.validate(package(dex=bytes(dex)))["reason"], "invalid-dex")

    def test_malformed_declared_hash_is_rejected_before_downloading(self):
        for value in ("sha256-invalid", "", 123):
            with self.subTest(value=value), patch.object(self.scanner.requests, "get") as download:
                result = self.scanner.validate_plugin_file(dict(self.plugin, fileHash=value), {})
                self.assertEqual(result["reason"], "invalid-file-hash")
                download.assert_not_called()

    def test_missing_hash_remains_optional(self):
        result = self.validate(package())
        self.assertTrue(result["ok"])
        self.assertFalse(result["hash_checked"])

    def test_wrong_hash_and_non_zip_are_still_rejected(self):
        self.assertEqual(self.validate(package(), fileHash="0" * 64)["reason"], "hash-mismatch")
        self.assertEqual(self.validate(b"not a ZIP")["reason"], "bad-zip-file")


if __name__ == "__main__":
    unittest.main()
