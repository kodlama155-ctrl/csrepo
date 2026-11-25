"""Read provider addresses declared in plugin bytecode and domain lists."""

from __future__ import annotations

import re
import struct
import threading
from urllib.parse import urlparse

import requests


IGNORED_PROVIDER_DOMAINS = {
    "schema.org", "w3.org", "android.com", "google.com", "googleapis.com",
    "github.com", "raw.githubusercontent.com", "gitlab.com", "cloudflare.com",
    "xml.org", "apache.org", "kotlinlang.org", "googletagmanager.com",
    "facebook.com", "twitter.com", "instagram.com", "youtube.com", "example.com",
    "127.0.0.1", "localhost", "jikan.moe", "themoviedb.org", "tmdb.org",
    "jsdelivr.net", "wikimedia.org", "invidious.io", "strawpoll.com", "f5.si",
    "wsrv.nl", "weserv.nl", "statically.io", "tinyurl.com", "bit.ly", "imgur.com",
    "vidmoly.to", "vidmoly.net", "vidmoly.biz", "vidmoly.me", "fembed.com",
    "mixdrop.co", "streamtape.com", "uqload.com", "filemoon.sx", "doodstream.com",
    "res.cloudinary.com", "bp.blogspot.com",
}


DYNAMIC_DOMAIN_LIST_CACHE: dict[str, dict[str, str]] = {}
DYNAMIC_DOMAIN_LOCK = threading.Lock()


DOMAIN_LIST_URL_KEYS = ("url", "canonical", "mainUrl", "candidates", "gateways", "domains", "mirrors")
DOMAIN_LIST_CONTAINERS = ("sources", "providers")


def domain_list_pairs(document) -> list[tuple[str, str]]:
    """(name, first address) pairs from the JSON domain-list layouts plugins publish:
    {"Name": "https://site"}, {"Name": ["https://site", ...]},
    {"key": {"name": "Name", "url": "https://site"}}, {"providers": {"Name": {"canonical": ...}}},
    {"sources": {"name": {"candidates": [...]}}}."""
    if not isinstance(document, dict):
        return []

    def first_url(value):
        if isinstance(value, str):
            return value.strip() if value.strip().startswith(("http://", "https://")) else None
        if isinstance(value, list):
            return next((url for item in value if isinstance(item, str) and (url := first_url(item))), None)
        if isinstance(value, dict):
            return next((url for key in DOMAIN_LIST_URL_KEYS if (url := first_url(value.get(key)))), None)
        return None

    pairs = []
    for container in DOMAIN_LIST_CONTAINERS:
        pairs.extend(domain_list_pairs(document.get(container)))
    for key, value in document.items():
        # Structural keys ("mirrors", "sources", ...) are not plugin names.
        if key in DOMAIN_LIST_CONTAINERS or key in DOMAIN_LIST_URL_KEYS or not (url := first_url(value)):
            continue
        pairs.append((str(key), url))
        if isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"].strip():
            pairs.append((value["name"], url))
    return pairs


def get_dynamic_domain_list(url: str) -> dict[str, str]:
    with DYNAMIC_DOMAIN_LOCK:
        if url in DYNAMIC_DOMAIN_LIST_CACHE:
            return DYNAMIC_DOMAIN_LIST_CACHE[url]
    mapping: dict[str, str] = {}
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code == 200:
            if urlparse(url).path.lower().endswith(".json"):
                try:
                    document = r.json()
                except ValueError:
                    document = None
                pairs = domain_list_pairs(document)
            else:
                lines = (line.strip().lstrip("|") for line in r.text.splitlines())
                pairs = [line.split(":", 1) for line in lines if ":" in line]
            for k, v in pairs:
                k_norm = re.sub(r"[^a-zA-Z0-9]", "", k.lower())
                v_clean = v.strip()
                if not v_clean.startswith("http"):
                    v_clean = "https://" + v_clean.lstrip("/")
                mapping[k_norm] = v_clean
    except Exception:
        pass
    with DYNAMIC_DOMAIN_LOCK:
        DYNAMIC_DOMAIN_LIST_CACHE[url] = mapping
    return mapping


def dex_provider_metadata(data: bytes, *, related_to: set[str] | None = None):
    """Read literal MainAPI.mainUrl assignments/getters without executing DEX code."""
    if len(data) < 112 or not data.startswith(b"dex\n"):
        return [], {}
    order = ">" if data[40:44] == b"\x12\x34\x56\x78" else "<"

    def uint(offset):
        return struct.unpack_from(order + "I", data, offset)[0]

    def ushort(offset):
        return struct.unpack_from(order + "H", data, offset)[0]

    def leb(offset):
        value = 0
        for shift in range(0, 35, 7):
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value, offset
        raise ValueError("Invalid DEX integer")

    def table(header, width):
        count, offset = uint(header), uint(header + 4)
        if count and (offset < 112 or offset > len(data) or count > (len(data) - offset) // width):
            raise ValueError("Invalid DEX table")
        return range(offset, offset + count * width, width)

    try:
        strings = []
        for item in table(56, 4):
            _, start = leb(uint(item))
            end = data.find(b"\0", start)
            if end < 0:
                raise ValueError("Unterminated DEX string")
            strings.append(data[start:end].decode("utf-8", errors="replace"))
        types = [strings[uint(item)] for item in table(64, 4)]
        fields = [(types[ushort(item)], strings[uint(item + 4)]) for item in table(80, 8)]
        field_types = [types[ushort(item + 2)] for item in table(80, 8)]
        methods = [(types[ushort(item)], strings[uint(item + 4)]) for item in table(88, 8)]
        classes = {}
        for item in table(96, 32):
            parent = uint(item + 8)
            classes[types[uint(item)]] = (types[parent] if parent != 0xFFFFFFFF else "", uint(item + 24))

        def provider_class(name):
            seen = set()
            while name and name not in seen:
                if name == "Lcom/lagradost/cloudstream3/MainAPI;":
                    return True
                seen.add(name)
                name = classes.get(name, ("", 0))[0]
            return False

        widths = [0] * 256
        for start, end, width in ((0x0A, 0x13, 1), (0x1D, 0x1F, 1), (0x1F, 0x21, 2),
                                   (0x2D, 0x3E, 2), (0x44, 0x6E, 2), (0x6E, 0x73, 3),
                                   (0x74, 0x79, 3), (0x7B, 0x90, 1), (0x90, 0xB0, 2),
                                   (0xB0, 0xD0, 1), (0xD0, 0xE3, 2)):
            widths[start:end] = [width] * (end - start)
        for op, width in {0: 1, 1: 1, 2: 2, 3: 3, 4: 1, 5: 2, 6: 3, 7: 1, 8: 2, 9: 3,
                          0x13: 2, 0x14: 3, 0x15: 2, 0x16: 2, 0x17: 3, 0x18: 5, 0x19: 2,
                          0x1A: 2, 0x1B: 3, 0x1C: 2, 0x21: 1, 0x22: 2, 0x23: 2,
                          0x24: 3, 0x25: 3, 0x26: 3, 0x27: 1, 0x28: 1, 0x29: 2,
                          0x2A: 3, 0x2B: 3, 0x2C: 3, 0xFA: 4, 0xFB: 4, 0xFC: 3,
                          0xFD: 3, 0xFE: 2, 0xFF: 2}.items():
            widths[op] = width

        def instructions(code):
            count = uint(code + 12)
            if count > (len(data) - code - 16) // 2:
                raise ValueError("Invalid DEX instructions")
            words = struct.unpack_from(order + "H" * count, data, code + 16)
            position = 0
            while position < len(words):
                word = words[position]
                op, width = word & 255, widths[word & 255]
                if not width or position + width > len(words) or (op == 0 and word != 0):
                    break
                yield op, word, words[position + 1:position + width]
                position += width

        def literal_urls(code, method):
            receiver = ushort(code) - ushort(code + 2)
            pending, assigned = None, None
            found = []
            for op, word, operands in instructions(code):
                if op in (0x1A, 0x1B):
                    index = operands[0]
                    if op == 0x1B:
                        index |= operands[1] << 16
                    pending = word >> 8, strings[index]
                else:
                    if op == 0x5B and word >> 12 == receiver and fields[operands[0]][1] == "mainUrl":
                        register = (word >> 8) & 15
                        assigned = pending[1] if pending is not None and pending[0] == register else None
                    elif pending is not None and op == 0x11 and method == "getMainUrl" and word >> 8 == pending[0]:
                        found.append(pending[1])
                    pending = None
            if method == "<init>" and assigned is not None:
                found.append(assigned)
            return found

        def class_methods(offset):
            if not offset:
                return
            counts = []
            for _ in range(4):
                count, offset = leb(offset)
                counts.append(count)
            if sum(counts[:2]) > len(fields) or sum(counts[2:]) > len(methods):
                raise ValueError("Invalid DEX class data")
            for _ in range(sum(counts[:2])):
                _, offset = leb(offset)
                _, offset = leb(offset)
            for count in counts[2:]:
                index = 0
                for _ in range(count):
                    delta, offset = leb(offset)
                    index += delta
                    _, offset = leb(offset)
                    code, offset = leb(offset)
                    yield *methods[index], code

        selected_classes = classes
        if related_to is not None:
            # Follow local class references, including intermediate aggregators/lambdas.
            # Unreferenced providers and arbitrary URL strings are not fallback sites.
            selected_classes = {}
            pending = list(related_to)
            while pending:
                name = pending.pop()
                if name not in classes or name in selected_classes:
                    continue
                parent, offset = selected_classes[name] = classes[name]
                pending.append(parent)
                for index, (owner, _) in enumerate(fields):
                    if owner == name:
                        pending.append(field_types[index])
                for owner, _, code in class_methods(offset):
                    if owner != name or not code:
                        continue
                    for op, _, operands in instructions(code):
                        if op in (0x1C, 0x1F, 0x20, 0x22, 0x23, 0x24, 0x25):
                            pending.append(types[operands[0]])
                        elif 0x52 <= op <= 0x6D:
                            index = operands[0]
                            pending.extend((fields[index][0], field_types[index]))
                        elif 0x6E <= op <= 0x72 or 0x74 <= op <= 0x78:
                            pending.append(methods[operands[0]][0])

        declared = {}
        for name, (_, offset) in selected_classes.items():
            if not provider_class(name):
                continue
            known = any(owner == name and field == "mainUrl" for owner, field in fields)
            found = []
            for owner, method, code in class_methods(offset):
                if owner == name and method == "getMainUrl":
                    known = True
                if code and owner == name and method in ("<init>", "getMainUrl"):
                    found.extend(literal_urls(code, method))
            if known or found:
                declared[name] = found
        return strings, declared
    except (IndexError, ValueError, struct.error):
        return [], {}


def provider_site_url(value: str) -> str | None:
    try:
        parsed = urlparse(value)
        domain = (parsed.hostname or "").lower()
        if (parsed.scheme not in ("http", "https") or "." not in domain or len(domain) <= 4
                or any(char.isspace() for char in value)
                or any(domain == ign or domain.endswith("." + ign) for ign in IGNORED_PROVIDER_DOMAINS)):
            return None
        if (re.search(r"\.(?:png|jpe?g|gif|webp|svg|ico|avif)(?:$|/)", parsed.path, re.IGNORECASE)
                or any(part in parsed.path.lower() for part in ("/picons/", "/logos/", "/image/fetch/", "/image/upload/", "/favicon"))):
            return None
        return value.rstrip("/") if not parsed.query else value
    except ValueError:
        return None


def is_playlist_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (parsed.scheme == "https" and "." in (parsed.hostname or "")
            and parsed.path.lower().endswith((".m3u", ".m3u8")) and not any(c.isspace() for c in value))


def extract_provider_urls(dex_bytes: bytes, plugin_name: str, internal_name: str = "", icon_url: str = "") -> tuple[list[str], bool]:
    strings, declared = dex_provider_metadata(dex_bytes)
    if not strings:
        if len(dex_bytes) >= 112 and dex_bytes.startswith(b"dex\n") and any(dex_bytes[56:60]):
            return [], False
        strings = [value.decode("ascii", errors="ignore")
                   for value in re.findall(rb'https?://[^\x00\s<>"\x27]+', dex_bytes)]
    values = [value for value in strings if value.startswith(("https://", "http://"))]
    names = {re.sub(r"[^a-z0-9]", "", value.lower()) for value in (plugin_name, internal_name) if value}
    names |= {name.removesuffix("provider") for name in tuple(names)}
    names.discard("")
    dynamic_urls = set()
    for value in values:
        try:
            filename = urlparse(value).path.rsplit("/", 1)[-1].lower()
            if filename.endswith((".txt", ".json")) and ("domain" in filename or "adres" in filename) and "proxy" not in filename:
                dynamic_urls.add(value)
        except ValueError:
            continue
    mapped = set()
    for url in sorted(dynamic_urls):
        mapping = get_dynamic_domain_list(url)
        for name in names:
            target = provider_site_url(mapping.get(name, ""))
            if target:
                mapped.add(target)
    if mapped:
        return sorted(mapped), False

    matching = {owner: urls for owner, urls in declared.items()
                if re.sub(r"[^a-z0-9]", "", owner.rsplit("/", 1)[-1].rstrip(";").lower()).removesuffix("provider") in names}
    selected = matching if matching else declared
    sites = {site for urls in selected.values() for value in urls if (site := provider_site_url(value))}
    if matching and not sites:
        _, related = dex_provider_metadata(dex_bytes, related_to=set(matching))
        sites = {site for urls in related.values() for value in urls if (site := provider_site_url(value))}
    if sites:
        return sorted(sites), False
    # Playlist providers (mainUrl is an .m3u file, often on GitHub): the playlist is what must open.
    playlists = {value for urls in selected.values() for value in urls if is_playlist_url(value)}
    if playlists:
        return sorted(playlists), False
    if dynamic_urls:
        return [], True
    if declared:
        return [], False

    candidates = set()
    for value in values:
        site = provider_site_url(value)
        if site:
            parsed = urlparse(site)
            candidates.add(f"{parsed.scheme}://{parsed.netloc}")
    candidates = sorted(candidates)
    matched = [url for url in candidates if any(name and name in url.lower() for name in names)]
    return (matched if matched else candidates[:4]), False
