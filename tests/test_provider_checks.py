import socket
import unittest
from unittest.mock import patch

import requests

from scanner import plugin_validation, provider_addresses, provider_checks
from test_plugin_validation import DownloadResponse, package




class ProviderCheckTests(unittest.TestCase):
    def test_package_names_are_not_provider_addresses(self):
        urls, missing = provider_addresses.extract_provider_urls(
            b"com.turkishw.provider\0com.berkstream.vendor.kraptor.tv\0", "Example"
        )
        self.assertEqual(urls, [])
        self.assertFalse(missing)

    def test_icon_address_does_not_prove_provider_address(self):
        urls, missing = provider_addresses.extract_provider_urls(b"", "Example", icon_url="https://unrelated.invalid/icon.png")
        self.assertEqual(urls, [])

    def test_explicit_url_keeps_original_scheme_and_port(self):
        urls, missing = provider_addresses.extract_provider_urls(b"http://provider.invalid:8080/path\0", "Example")
        self.assertEqual(urls, ["http://provider.invalid:8080"])

    def test_provider_results_are_repeatable(self):
        data = b"https://z-provider.invalid\0https://a-provider.invalid\0"
        first, _ = provider_addresses.extract_provider_urls(data, "Example")
        self.assertEqual(first, sorted(first))

    def validate(self, urls, dynamic_missing=False, probe=None):
        plugin = {"name": "Example", "internalName": "Example", "status": 1, "url": "https://example.invalid/Example.cs3"}
        with patch.object(requests, "get", return_value=DownloadResponse(package())), \
             patch.object(provider_addresses, "extract_provider_urls", return_value=(urls, dynamic_missing)), \
             patch.object(provider_checks, "is_provider_dead", return_value=probe or (False, "no-urls")):
            return plugin_validation.validate_plugin_file(plugin, {}, domain_cache={})

    def test_no_urls_is_reported_as_unverified(self):
        result = self.validate([])
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("provider_check"), "unverified")

    def test_failed_probe_keeps_file_integrity_and_site_results_separate(self):
        # Embedded URLs do not establish the address actually used at runtime.
        result = self.validate(["https://provider.invalid"], probe=(True, "provider.invalid:unreachable"))
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("provider_check"), "unverified")
        self.assertIn("unreachable", result.get("provider_reason", ""))

    def test_missing_dynamic_mapping_is_not_claimed_as_dead_provider(self):
        result = self.validate([], dynamic_missing=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("provider_check"), "unverified")

    def test_reachable_candidate_is_reported_separately_from_file_integrity(self):
        result = self.validate(["https://provider.invalid"], probe=(False, "provider.invalid:alive"))
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("provider_check"), "reachable")

    def test_http_access_denial_is_not_reported_as_reachable(self):
        response = requests.Response()
        response.status_code = 403
        response._content = b""
        with patch.object(provider_checks, "TR_PROBE_ENABLED", False), \
             patch.object(provider_checks, "query_ttnet_dns", return_value=[]), \
             patch.object(socket, "gethostbyname", return_value="192.0.2.1"), \
             patch.object(requests, "get", return_value=response):
            failed, reason = provider_checks.is_provider_dead(["https://provider.invalid"], {})
        self.assertTrue(failed)
        self.assertIn("http-403", reason)

    def test_different_protocols_and_ports_have_separate_probe_results(self):
        first = requests.Response()
        first.status_code = 404
        first._content = b""
        second = requests.Response()
        second.status_code = 200
        second._content = b"Available"
        with patch.object(provider_checks, "query_ttnet_dns", return_value=[]), \
             patch.object(socket, "gethostbyname", return_value="192.0.2.1"), \
             patch.object(requests, "get", side_effect=[first, second]) as download:
            failed, reason = provider_checks.is_provider_dead(
                ["http://provider.invalid:8080", "https://provider.invalid"], {}
            )
        self.assertFalse(failed)
        self.assertEqual(download.call_count, 2)


if __name__ == "__main__":
    unittest.main()
