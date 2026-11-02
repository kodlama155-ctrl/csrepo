import hashlib
import importlib.util
import os
import struct
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch


SCANNER_PATH = Path(__file__).resolve().parents[1] / "scanner" / "discover.py"
DOMAIN_LIST = "https://raw.githubusercontent.com/example/domains/main/eklenti_domainleri.txt"


def uleb(value):
    output = bytearray()
    while True:
        byte = value & 127
        value >>= 7
        output.append(byte | (128 if value else 0))
        if not value:
            return bytes(output)


def declared_url_dex(url, extra_urls=(), getter=False, provider=True, overwrite=False):
    """Small DEX with a real class, field and method table; it is never executed."""
    owner = "Lexample/Provider;"
    parent = "Lcom/lagradost/cloudstream3/MainAPI;" if provider else "Ljava/lang/Object;"
    method = "getMainUrl" if getter else "<init>"
    values = sorted({owner, parent, "Ljava/lang/String;", "V", "mainUrl", method,
                     *(extra_urls), *([url] if url else [])})
    strings = {value: index for index, value in enumerate(values)}
    types = sorted({owner, parent, "Ljava/lang/String;", "V"}, key=strings.get)
    type_ids = {value: index for index, value in enumerate(types)}
    data = bytearray(112)
    data[:8] = b"dex\n035\0"
    string_off = len(data); data.extend(bytes(4 * len(values)))
    type_off = len(data); data.extend(bytes(4 * len(types)))
    proto_off = len(data); data.extend(struct.pack("<III", strings["V"], type_ids["V"], 0))
    field_off = len(data); data.extend(struct.pack("<HHI", type_ids[owner], type_ids["Ljava/lang/String;"], strings["mainUrl"]))
    method_off = len(data); data.extend(struct.pack("<HHI", type_ids[owner], 0, strings[method]))
    class_off = len(data); data.extend(bytes(32))
    for index, value in enumerate(values):
        struct.pack_into("<I", data, string_off + 4 * index, len(data))
        data.extend(uleb(len(value)) + value.encode("utf-8") + b"\0")
    for index, value in enumerate(types):
        struct.pack_into("<I", data, type_off + 4 * index, strings[value])
    while len(data) % 4:
        data.append(0)
    code_off = len(data)
    if url:
        instructions = [0x001A, strings[url], 0x0011] if getter else [0x001A, strings[url], 0x105B, 0, 0x000E]
        if overwrite and not getter:
            instructions[-1:] = [0x0012, 0x105B, 0, 0x000E]
    else:
        instructions = [0x0012, 0x105B, 0, 0x000E]
    data.extend(struct.pack("<HHHHII", 2, 1, 0, 0, 0, len(instructions)))
    data.extend(struct.pack("<" + "H" * len(instructions), *instructions))
    class_data = len(data)
    data.extend(bytes([0, 1, 0 if getter else 1, 1 if getter else 0]))
    data.extend(uleb(0) + uleb(2))
    data.extend(uleb(0) + uleb(1) + uleb(code_off))
    struct.pack_into("<IIIIIIII", data, class_off, type_ids[owner], 1, type_ids[parent], 0, 0xFFFFFFFF, 0, class_data, 0)
    for offset, count, table in ((56, len(values), string_off), (64, len(types), type_off),
                                 (72, 1, proto_off), (80, 1, field_off), (88, 1, method_off), (96, 1, class_off)):
        struct.pack_into("<II", data, offset, count, table)
    struct.pack_into("<III", data, 32, len(data), 112, 0x12345678)
    data[12:32] = hashlib.sha1(data[32:]).digest()
    data[8:12] = zlib.adler32(data[12:]).to_bytes(4, "little")
    return bytes(data)


def linked_provider_dex(wrapper_url, child_urls=("https://source.invalid",), *, connected=True, cycle=False):
    """Wrapper -> helper -> providers, with another provider that is never referenced."""
    wrapper, bridge = "Lexample/WrapperProvider;", "Lexample/Bridge;"
    children = [f"Lexample/Child{index}Provider;" for index in range(len(child_urls))]
    definitions = {
        wrapper: (wrapper_url, True, [bridge] if connected else []),
        bridge: ("https://helper.invalid", False, children + ([wrapper] if cycle else [])),
        "Lexample/UnusedProvider;": ("https://unused.invalid", True, []),
        **{owner: (url, True, []) for owner, url in zip(children, child_urls)},
    }
    api, obj, string = "Lcom/lagradost/cloudstream3/MainAPI;", "Ljava/lang/Object;", "Ljava/lang/String;"
    values = sorted({*definitions, api, obj, string, "V", "mainUrl", "<init>",
                     *(url for url, _, _ in definitions.values() if url)})
    strings = {value: index for index, value in enumerate(values)}
    types = sorted({*definitions, api, obj, string, "V"}, key=strings.get)
    type_ids = {value: index for index, value in enumerate(types)}
    owners = sorted(definitions, key=type_ids.get)
    owner_ids = {owner: index for index, owner in enumerate(owners)}
    data = bytearray(112)
    data[:8] = b"dex\n035\0"
    string_off = len(data); data.extend(bytes(4 * len(values)))
    type_off = len(data); data.extend(bytes(4 * len(types)))
    proto_off = len(data); data.extend(struct.pack("<III", strings["V"], type_ids["V"], 0))
    field_off = len(data)
    for owner in owners:
        data.extend(struct.pack("<HHI", type_ids[owner], type_ids[string], strings["mainUrl"]))
    method_off = len(data)
    for owner in owners:
        data.extend(struct.pack("<HHI", type_ids[owner], 0, strings["<init>"]))
    class_off = len(data); data.extend(bytes(32 * len(owners)))
    for index, value in enumerate(values):
        struct.pack_into("<I", data, string_off + 4 * index, len(data))
        data.extend(uleb(len(value)) + value.encode() + b"\0")
    for index, value in enumerate(types):
        struct.pack_into("<I", data, type_off + 4 * index, strings[value])
    for owner in owners:
        index = owner_ids[owner]
        url, provider, calls = definitions[owner]
        while len(data) % 4:
            data.append(0)
        code_off = len(data)
        instructions = [0x001A, strings[url]] if url else [0x0012]
        instructions += [0x105B, index]
        for target in calls:
            instructions += [0x0022, type_ids[target], 0x1070, owner_ids[target], 0]
        instructions.append(0x000E)
        data.extend(struct.pack("<HHHHII", 2, 1, 1, 0, 0, len(instructions)))
        data.extend(struct.pack("<" + "H" * len(instructions), *instructions))
        class_data = len(data)
        data.extend(bytes([0, 1, 1, 0]))
        data.extend(uleb(index) + uleb(2))
        data.extend(uleb(index) + uleb(0x10001) + uleb(code_off))
        struct.pack_into("<IIIIIIII", data, class_off + 32 * index,
                         type_ids[owner], 1, type_ids[api if provider else obj], 0, 0xFFFFFFFF, 0, class_data, 0)
    for offset, count, table in ((56, len(values), string_off), (64, len(types), type_off),
                                 (72, 1, proto_off), (80, len(owners), field_off),
                                 (88, len(owners), method_off), (96, len(owners), class_off)):
        struct.pack_into("<II", data, offset, count, table)
    struct.pack_into("<III", data, 32, len(data), 112, 0x12345678)
    data[12:32] = hashlib.sha1(data[32:]).digest()
    data[8:12] = zlib.adler32(data[12:]).to_bytes(4, "little")
    return bytes(data)


class ProviderAddressTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("provider_addresses", SCANNER_PATH)
        self.scanner = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            spec.loader.exec_module(self.scanner)
        self.addCleanup(self.scanner.SESSION.close)

    def test_missing_domain_list_never_falls_back_to_cloudinary(self):
        dex = (DOMAIN_LIST + "\0https://res.cloudinary.com/example/image/fetch/f_auto/\0").encode()
        with patch.object(self.scanner, "get_dynamic_domain_list", return_value={}):
            urls, missing = self.scanner.extract_provider_urls(dex, "HintDizi")
        self.assertEqual(urls, [])
        self.assertTrue(missing)

    def test_dynamic_mapping_takes_priority_over_old_embedded_address(self):
        dex = (DOMAIN_LIST + "\0https://example-old.invalid\0").encode()
        with patch.object(self.scanner, "get_dynamic_domain_list", return_value={"example": "https://fresh.invalid/base"}):
            urls, missing = self.scanner.extract_provider_urls(dex, "Example")
        self.assertEqual(urls, ["https://fresh.invalid/base"])
        self.assertFalse(missing)

    def test_json_domain_list_address_is_used(self):
        json_list = "https://raw.githubusercontent.com/example/domains/main/domains.json"
        dex = (json_list + "\0https://example-old.invalid\0").encode()
        with patch.object(self.scanner, "get_dynamic_domain_list", return_value={"example": "https://fresh.invalid"}) as load:
            urls, missing = self.scanner.extract_provider_urls(dex, "Example")
        load.assert_called_once_with(json_list)
        self.assertEqual(urls, ["https://fresh.invalid"])
        self.assertFalse(missing)

    def test_json_domain_list_reads_published_layouts_but_not_structural_keys(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "_updated": "2026-10-30T04:50:35Z",
            "schemaVersion": 1,
            "Example Site": "https://fresh.invalid",
            "Nested": {"candidates": ["https://nested.invalid", "https://nested2.invalid"]},
            "Listed": ["not-a-url", "https://listed.invalid"],
            "hdfilm": {"name": "HDFilm Plus", "url": "https://hd.invalid", "status": "ONLINE"},
            "sources": {"sport": {"gateways": ["https://sport.invalid/"]}},
            "providers": {"Kult": {"canonical": "https://kult.invalid", "allowedHosts": ["kult.invalid"]}},
            "mirrors": ["https://mirror.invalid"],
        }
        with patch.object(self.scanner.requests, "get", return_value=response):
            mapping = self.scanner.get_dynamic_domain_list("https://example.invalid/domains.json")
        self.assertEqual(mapping, {
            "examplesite": "https://fresh.invalid", "nested": "https://nested.invalid",
            "listed": "https://listed.invalid", "hdfilm": "https://hd.invalid", "hdfilmplus": "https://hd.invalid",
            "sport": "https://sport.invalid/", "kult": "https://kult.invalid",
        })

    def test_playlist_provider_is_checked_by_its_playlist_url(self):
        playlist = "https://raw.githubusercontent.com/example/lists/main/tv.m3u"
        urls, missing = self.scanner.extract_provider_urls(declared_url_dex(playlist), "Example")
        self.assertEqual((urls, missing), ([playlist], False))

    def test_invalid_json_domain_list_has_no_addresses(self):
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("not json")
        with patch.object(self.scanner.requests, "get", return_value=response):
            self.assertEqual(self.scanner.get_dynamic_domain_list("https://example.invalid/domains.json"), {})

    def test_image_hosts_and_image_paths_are_not_sites(self):
        dex = (b"https://res.cloudinary.com/demo/image/fetch/\0"
               b"https://1.bp.blogspot.com/abc/image.png\0"
               b"http://192.0.2.1/picons/logos/channel.png\0"
               b"https://pictures.invalid/icon.webp\0")
        urls, missing = self.scanner.extract_provider_urls(dex, "Example")
        self.assertEqual(urls, [])
        self.assertFalse(missing)

    def test_site_is_kept_when_package_also_contains_images(self):
        dex = b"https://provider.invalid\0https://res.cloudinary.com/demo/image/fetch/\0"
        self.assertEqual(self.scanner.extract_provider_urls(dex, "Example")[0], ["https://provider.invalid"])

    def test_declared_site_takes_priority_over_helper_with_same_name(self):
        dex = declared_url_dex("https://api.gain.tv", ["https://gain.la.drm.cloud"])
        self.assertEqual(self.scanner.extract_provider_urls(dex, "Gain")[0], ["https://api.gain.tv"])

    def test_declared_site_preserves_scheme_port_and_base_path(self):
        dex = declared_url_dex("http://provider.invalid:8080/base", ["https://helper.invalid"])
        self.assertEqual(self.scanner.extract_provider_urls(dex, "Provider")[0], ["http://provider.invalid:8080/base"])

    def test_literal_site_getter_is_read(self):
        dex = declared_url_dex("https://provider.invalid", ["https://helper.invalid"], getter=True)
        self.assertEqual(self.scanner.extract_provider_urls(dex, "Provider")[0], ["https://provider.invalid"])

    def test_unresolved_site_field_does_not_select_unrelated_helper(self):
        dex = declared_url_dex(None, ["https://helper.invalid"])
        self.assertEqual(self.scanner.extract_provider_urls(dex, "Provider")[0], [])

    def test_later_unresolved_assignment_invalidates_old_literal_address(self):
        dex = declared_url_dex("https://old-provider.invalid", overwrite=True)
        self.assertEqual(self.scanner.extract_provider_urls(dex, "Provider")[0], [])

    def test_helper_class_main_url_is_not_a_provider_declaration(self):
        dex = declared_url_dex("https://helper.invalid", provider=False)
        strings, declared = self.scanner.dex_provider_metadata(dex)
        self.assertTrue(strings)
        self.assertEqual(declared, {})

    def test_invalid_table_offsets_do_not_crash_address_detection(self):
        dex = bytearray(declared_url_dex("https://provider.invalid"))
        struct.pack_into("<I", dex, 60, len(dex) + 100)
        urls, missing = self.scanner.extract_provider_urls(bytes(dex), "Provider")
        self.assertEqual(urls, [])
        self.assertFalse(missing)

    def test_aggregators_follow_referenced_providers_through_helper_classes(self):
        for wrapper_url in ("https://raw.githubusercontent.com/example/project/main/", "https://www.themoviedb.org"):
            with self.subTest(wrapper_url=wrapper_url):
                dex = linked_provider_dex(wrapper_url, ("https://first.invalid/base", "https://second.invalid"))
                urls, missing = self.scanner.extract_provider_urls(dex, "0 Wrapper", "Wrapper")
                self.assertEqual(urls, ["https://first.invalid/base", "https://second.invalid"])
                self.assertFalse(missing)

    def test_unreferenced_provider_cannot_supply_a_wrapper_site(self):
        dex = linked_provider_dex("https://github.com/example/project", connected=False)
        self.assertEqual(self.scanner.extract_provider_urls(dex, "0 Wrapper", "Wrapper"), ([], False))

    def test_unresolved_wrapper_can_use_its_referenced_provider(self):
        dex = linked_provider_dex(None)
        self.assertEqual(self.scanner.extract_provider_urls(dex, "0 Wrapper", "Wrapper"),
                         (["https://source.invalid"], False))

    def test_resolved_primary_site_keeps_priority_over_referenced_providers(self):
        dex = linked_provider_dex("https://primary.invalid")
        self.assertEqual(self.scanner.extract_provider_urls(dex, "0 Wrapper", "Wrapper"),
                         (["https://primary.invalid"], False))

    def test_reference_cycle_terminates_without_selecting_unrelated_sites(self):
        dex = linked_provider_dex("https://www.themoviedb.org", cycle=True)
        self.assertEqual(self.scanner.extract_provider_urls(dex, "0 Wrapper", "Wrapper"),
                         (["https://source.invalid"], False))


if __name__ == "__main__":
    unittest.main()
