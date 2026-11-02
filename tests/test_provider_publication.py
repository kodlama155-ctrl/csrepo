import hashlib
import importlib.util
import json
import os
import socket
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import requests

from test_plugin_validation import DownloadResponse, dex_fixture, package
from test_provider_addresses import linked_provider_dex


SCANNER_PATH = Path(__file__).resolve().parents[1] / "scanner" / "discover.py"


def provider_package(url):
    data = bytearray(dex_fixture() + url.encode("ascii") + b"\0")
    data[32:36] = len(data).to_bytes(4, "little")
    data[12:32] = hashlib.sha1(data[32:]).digest()
    data[8:12] = zlib.adler32(data[12:]).to_bytes(4, "little")
    return package(dex=bytes(data))


def site_response(status=200, body=b"Available"):
    response = requests.Response()
    response.status_code = status
    response._content = body
    response.encoding = "utf-8"
    return response


class ProviderPublicationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("provider_publication", SCANNER_PATH)
        self.scanner = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            spec.loader.exec_module(self.scanner)
        self.addCleanup(self.scanner.SESSION.close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scanner.ROOT = Path(temporary.name)
        self.scanner.DATA = self.scanner.ROOT / "data"
        self.scanner.DATA.mkdir()
        self.repo = {"repository": "example/repo", "plugin_lists": [{"url": "https://example.invalid/plugins.json"}]}
        self.good = {"name": "Good", "internalName": "Good", "language": "tr", "status": 1,
                     "version": 1, "url": "https://example.invalid/good.cs3"}
        self.bad = {"name": "Blocked", "internalName": "Blocked", "language": "tr", "status": 1,
                    "version": 1, "url": "https://example.invalid/blocked.cs3"}
        self.good_site = "https://good-provider.invalid"
        self.bad_site = "https://blocked-provider.invalid"

    def build(self, plugins, packages, blocked_response=None, dns_failure=False):
        def get(url, **kwargs):
            if url in packages:
                return DownloadResponse(packages[url])
            if url == self.good_site:
                return site_response()
            if url == self.bad_site:
                if isinstance(blocked_response, Exception):
                    raise blocked_response
                return blocked_response if blocked_response is not None else site_response(403)
            raise AssertionError("Unexpected network request: " + url)

        def resolve(host):
            if dns_failure and host == "blocked-provider.invalid":
                raise socket.gaierror("Controlled DNS failure")
            return "192.0.2.1"

        with patch.object(self.scanner, "fetch_plugin_entries", return_value=plugins), \
             patch.object(self.scanner.requests, "get", side_effect=get), \
             patch.object(self.scanner, "query_ttnet_dns", return_value=[]), \
             patch.object(self.scanner.socket, "gethostbyname", side_effect=resolve):
            counts = self.scanner.build_cloudstream_bundle([self.repo], set())
        published = json.loads((self.scanner.ROOT / "plugins-x92.json").read_text(encoding="utf-8"))
        health = json.loads((self.scanner.DATA / "plugin-health.json").read_text(encoding="utf-8"))
        return counts, published, health

    def test_site_failures_do_not_enter_published_list(self):
        cases = [
            ("403", site_response(403), False, "http-403"),
            ("404", site_response(404), False, "http-404"),
            ("500", site_response(500), False, "http-500"),
            ("timeout", requests.Timeout("Controlled timeout"), False, "unreachable"),
            ("dns", None, True, "dns-error"),
            ("error-page", site_response(200, b"Siteye ulasilamiyor"), False, "error-page"),
            ("parked", site_response(200, b"This domain is for sale"), False, "parked-domain"),
        ]
        for label, response, dns_failure, expected in cases:
            with self.subTest(case=label):
                counts, published, health = self.build([self.good, self.bad], {
                    self.good["url"]: provider_package(self.good_site),
                    self.bad["url"]: provider_package(self.bad_site),
                }, response, dns_failure)
                self.assertEqual([p["internalName"] for p in published], ["Good"])
                self.assertEqual(counts, (1, 1, 0))
                rejected = next(row for row in health["plugins"] if row["internal_name"] == "blocked")
                self.assertEqual(rejected["status"], "rejected")
                self.assertEqual(rejected["attempts"][0]["result"], "provider-unverified")
                self.assertIn(expected, rejected["attempts"][0]["provider_reason"])
                self.assertEqual(health["provider_unverified_count"], 0)

    def test_plugin_without_site_address_is_published_as_unverified(self):
        counts, published, health = self.build([self.good, self.bad], {
            self.good["url"]: provider_package(self.good_site), self.bad["url"]: package(),
        })
        self.assertEqual(sorted(p["internalName"] for p in published), ["Blocked", "Good"])
        self.assertEqual(counts, (2, 0, 0))
        row = next(row for row in health["plugins"] if row["internal_name"] == "blocked")
        self.assertEqual((row["status"], row["provider_check"], row["provider_reason"]),
                         ("healthy", "unverified", "no-urls"))
        self.assertEqual(health["provider_unverified_count"], 1)

    def test_aggregator_is_published_only_when_a_referenced_provider_is_reachable(self):
        wrapper = dict(self.bad, name="0 Wrapper", internalName="Wrapper")
        archive = package(dex=linked_provider_dex("https://www.themoviedb.org", (self.bad_site,)))
        for status, expected in ((403, ["Good"]), (200, ["Good", "Wrapper"])):
            with self.subTest(status=status):
                _, published, health = self.build([self.good, wrapper], {
                    self.good["url"]: provider_package(self.good_site), wrapper["url"]: archive,
                }, blocked_response=site_response(status))
                self.assertEqual(sorted(p["internalName"] for p in published), expected)
                row = next(p for p in health["plugins"] if p["internal_name"] == "wrapper")
                if status == 403:
                    self.assertEqual(row["status"], "rejected")
                    self.assertIn("http-403", row["attempts"][0]["provider_reason"])
                else:
                    self.assertEqual(row["status"], "healthy")

    def test_reachable_older_variant_replaces_unverified_newer_variant(self):
        newer = dict(self.good, version=2, url="https://example.invalid/newer.cs3")
        counts, published, health = self.build([self.good, newer], {
            self.good["url"]: provider_package(self.good_site),
            newer["url"]: provider_package(self.bad_site),
        })
        self.assertEqual(published[0]["version"], 1)
        self.assertEqual(counts, (1, 0, 1))
        self.assertTrue(health["plugins"][0]["fallback_used"])
        self.assertEqual(health["rejection_attempt_counts"], {"provider-unverified": 1})
        self.assertFalse(health["checks"]["provider_checks_are_advisory"])
        self.assertTrue(health["checks"]["provider_reachable_required"])

    def test_missing_dynamic_address_is_published_as_unverified(self):
        with patch.object(self.scanner, "extract_provider_urls", side_effect=lambda data, name, *args:
                          ([self.good_site], False) if name == "Good" else ([], True)):
            counts, published, health = self.build([self.good, self.bad], {
                self.good["url"]: package(), self.bad["url"]: package(),
            })
        self.assertEqual(sorted(p["internalName"] for p in published), ["Blocked", "Good"])
        row = next(row for row in health["plugins"] if row["internal_name"] == "blocked")
        self.assertEqual((row["status"], row["provider_check"], row["provider_reason"]),
                         ("healthy", "unverified", "dynamic-domain-missing"))

    def test_all_unverified_plugins_preserve_existing_outputs(self):
        previous = self.scanner.ROOT / "plugins-x92.json"
        previous.write_text('[{"internalName":"Existing"}]\n', encoding="utf-8")
        before = {p.relative_to(self.scanner.ROOT): p.read_bytes() for p in self.scanner.ROOT.rglob("*") if p.is_file()}
        with self.assertRaises(RuntimeError):
            self.build([self.bad], {self.bad["url"]: provider_package(self.bad_site)})
        after = {p.relative_to(self.scanner.ROOT): p.read_bytes() for p in self.scanner.ROOT.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_file_only_success_cannot_bypass_site_requirement(self):
        def check(plugin, *args):
            result = {"ok": True, "reason": "ok"}
            if plugin["internalName"] == "Good":
                result["provider_check"] = "reachable"
            return result

        with patch.object(self.scanner, "fetch_plugin_entries", return_value=[self.good, self.bad]), \
             patch.object(self.scanner, "validate_plugin_file", side_effect=check):
            self.scanner.build_cloudstream_bundle([self.repo], set())
        published = json.loads((self.scanner.ROOT / "plugins-x92.json").read_text(encoding="utf-8"))
        self.assertEqual([p["internalName"] for p in published], ["Good"])


if __name__ == "__main__":
    unittest.main()
