import hashlib
import json
import socket
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import requests

from scanner import (
    plugin_bundle,
    plugin_validation,
    provider_addresses,
    provider_checks,
    repository_discovery,
    scan_utils,
)
from test_discover import use_scan_root
from test_plugin_validation import DownloadResponse, dex_fixture, package
from test_provider_addresses import linked_provider_dex



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
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        use_scan_root(self, Path(temporary.name))
        scan_utils.DATA.mkdir()
        self.repo = {"repository": "example/repo", "plugin_lists": [{"url": "https://example.invalid/plugins.json"}]}
        self.good = {"name": "Good", "internalName": "Good", "language": "tr", "status": 1,
                     "version": 1, "url": "https://example.invalid/good.cs3"}
        self.bad = {"name": "Blocked", "internalName": "Blocked", "language": "tr", "status": 1,
                    "version": 1, "url": "https://example.invalid/blocked.cs3"}
        self.good_site = "https://good-provider.invalid"
        self.bad_site = "https://blocked-provider.invalid"

    def build(self, plugins, packages, blocked_response=None, dns_failure=False,
              good_response=None, extra_sites=None):
        extra_sites = extra_sites or {}

        def get(url, **kwargs):
            if url in packages:
                return DownloadResponse(packages[url])
            if url == self.good_site:
                return good_response if good_response is not None else site_response()
            if url in extra_sites:
                return extra_sites[url]
            if url == self.bad_site:
                if isinstance(blocked_response, Exception):
                    raise blocked_response
                return blocked_response if blocked_response is not None else site_response(403)
            raise AssertionError("Unexpected network request: " + url)

        def resolve(host):
            if dns_failure and host == "blocked-provider.invalid":
                raise socket.gaierror("Controlled DNS failure")
            return "192.0.2.1"

        with patch.object(repository_discovery, "fetch_plugin_entries", return_value=plugins), \
             patch.object(requests, "get", side_effect=get), \
             patch.object(provider_checks, "TR_PROBE_ENABLED", False), \
             patch.object(provider_checks, "query_ttnet_dns", return_value=[]), \
             patch.object(socket, "gethostbyname", side_effect=resolve):
            counts = plugin_bundle.build_cloudstream_bundle([self.repo], set())
        published = json.loads((scan_utils.ROOT / "plugins-x92.json").read_text(encoding="utf-8"))
        health = json.loads((scan_utils.DATA / "plugin-health.json").read_text(encoding="utf-8"))
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
        with patch.object(provider_addresses, "extract_provider_urls", side_effect=lambda data, name, *args:
                          ([self.good_site], False) if name == "Good" else ([], True)):
            counts, published, health = self.build([self.good, self.bad], {
                self.good["url"]: package(), self.bad["url"]: package(),
            })
        self.assertEqual(sorted(p["internalName"] for p in published), ["Blocked", "Good"])
        row = next(row for row in health["plugins"] if row["internal_name"] == "blocked")
        self.assertEqual((row["status"], row["provider_check"], row["provider_reason"]),
                         ("healthy", "unverified", "dynamic-domain-missing"))

    def test_all_unverified_plugins_preserve_existing_outputs(self):
        previous = scan_utils.ROOT / "plugins-x92.json"
        previous.write_text('[{"internalName":"Existing"}]\n', encoding="utf-8")
        before = {p.relative_to(scan_utils.ROOT): p.read_bytes() for p in scan_utils.ROOT.rglob("*") if p.is_file()}
        with self.assertRaises(RuntimeError):
            self.build([self.bad], {self.bad["url"]: provider_package(self.bad_site)})
        after = {p.relative_to(scan_utils.ROOT): p.read_bytes() for p in scan_utils.ROOT.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_live_plugins_get_their_own_published_list(self):
        live = dict(self.good, name="Live Good", internalName="LiveGood",
                    url="https://example.invalid/live.cs3", tvTypes=["Live"])
        vod = dict(self.good, name="Vod", internalName="Vod",
                   url="https://example.invalid/vod.cs3", tvTypes=["Movie"])
        self.build([live, vod], {
            live["url"]: provider_package(self.good_site),
            vod["url"]: provider_package(self.good_site),
        })
        rows = json.loads((scan_utils.DATA / "live-plugins.json").read_text(encoding="utf-8"))
        self.assertEqual([row["internalName"] for row in rows], ["LiveGood"])

    def test_transient_site_error_keeps_previous_record_for_one_scan(self):
        archive = provider_package(self.good_site)
        digest = hashlib.sha256(archive).hexdigest()
        stable = dict(self.good, name="Stable", internalName="Stable",
                      url="https://example.invalid/stable.cs3")
        stable_site = "https://stable-provider.invalid"
        packages = {self.good["url"]: archive, stable["url"]: provider_package(stable_site)}
        extra = {stable_site: site_response()}

        scan_utils.save_json(scan_utils.ROOT / "plugins-x92.json", [
            dict(self.good, fileHash="sha256-" + digest),
        ])

        counts, published, health = self.build(
            [self.good, stable], packages, good_response=site_response(500), extra_sites=extra,
        )
        kept = next(p for p in published if p["internalName"] == "Good")
        self.assertTrue(kept["recheck"])
        self.assertEqual(kept["fileHash"], "sha256-" + digest)
        self.assertEqual(counts, (2, 0, 0))
        row = next(r for r in health["plugins"] if r["internal_name"] == "good")
        self.assertEqual(row["status"], "recheck")
        self.assertIn("http-500", row["reason"])
        self.assertEqual(health["recheck_plugin_count"], 1)

        # İkinci tarama: işaretli kayıt bir daha tutulmaz.
        counts, published, health = self.build(
            [self.good, stable], packages, good_response=site_response(500), extra_sites=extra,
        )
        self.assertEqual([p["internalName"] for p in published], ["Stable"])
        row = next(r for r in health["plugins"] if r["internal_name"] == "good")
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(health["recheck_plugin_count"], 0)

    def test_changed_file_or_non_transient_error_does_not_keep_previous_record(self):
        archive = provider_package(self.good_site)
        digest = hashlib.sha256(archive).hexdigest()
        stable = dict(self.good, name="Stable", internalName="Stable",
                      url="https://example.invalid/stable.cs3")
        stable_site = "https://stable-provider.invalid"
        packages = {self.good["url"]: archive, stable["url"]: provider_package(stable_site)}
        extra = {stable_site: site_response()}

        # Dosya değişmiş: eski kayıt tutulmaz.
        scan_utils.save_json(scan_utils.ROOT / "plugins-x92.json", [
            dict(self.good, fileHash="sha256-" + "0" * 64),
        ])
        _, published, _ = self.build(
            [self.good, stable], packages, good_response=site_response(500), extra_sites=extra,
        )
        self.assertEqual([p["internalName"] for p in published], ["Stable"])

        # Geçici olmayan hata (403): eski kayıt tutulmaz.
        scan_utils.save_json(scan_utils.ROOT / "plugins-x92.json", [
            dict(self.good, fileHash="sha256-" + digest),
        ])
        _, published, _ = self.build(
            [self.good, stable], packages, good_response=site_response(403), extra_sites=extra,
        )
        self.assertEqual([p["internalName"] for p in published], ["Stable"])

    def test_file_only_success_cannot_bypass_site_requirement(self):
        def check(plugin, *args):
            result = {"ok": True, "reason": "ok"}
            if plugin["internalName"] == "Good":
                result["provider_check"] = "reachable"
            return result

        with patch.object(repository_discovery, "fetch_plugin_entries", return_value=[self.good, self.bad]), \
             patch.object(plugin_validation, "validate_plugin_file", side_effect=check):
            plugin_bundle.build_cloudstream_bundle([self.repo], set())
        published = json.loads((scan_utils.ROOT / "plugins-x92.json").read_text(encoding="utf-8"))
        self.assertEqual([p["internalName"] for p in published], ["Good"])


if __name__ == "__main__":
    unittest.main()
