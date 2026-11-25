"""Check plugin download integrity, archive contents, and provider status."""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import zipfile
import zlib

import requests

from . import provider_addresses, provider_checks, scan_utils
from .plugin_metadata import normalize_sha256


PLUGIN_MAX_BYTES = 25 * 1024 * 1024


def _put_cache(cache: dict[str, dict], key: str, value: dict, lock: threading.Lock | None):
    if lock:
        with lock:
            cache[key] = value
    else:
        cache[key] = value


def load_blacklist() -> set[str]:
    p = scan_utils.DATA / "blacklist.json"
    if p.is_file():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {str(x).strip().lower() for x in data if x}
        except Exception:
            pass
    return set()


def valid_dex_header(data: bytes) -> bool:
    """Verify stable DEX headers and integrity fields without executing code."""
    if len(data) < 112 or not re.fullmatch(rb"dex\n0(?:35|37|38|39|40)\x00", data[:8]):
        return False
    endian = int.from_bytes(data[40:44], "little")
    if endian not in (0x12345678, 0x78563412):
        return False
    byteorder = "big" if endian == 0x78563412 else "little"
    return (
        int.from_bytes(data[32:36], byteorder) == len(data)
        and int.from_bytes(data[36:40], byteorder) == 112
        and data[12:32] == hashlib.sha1(data[32:]).digest()
        and int.from_bytes(data[8:12], byteorder) == zlib.adler32(data[12:])
    )


def validate_plugin_file(
    plugin: dict,
    cache: dict[str, dict],
    cache_lock: threading.Lock | None = None,
    domain_cache: dict[str, tuple[bool, str]] | None = None,
    domain_lock: threading.Lock | None = None,
    blacklist: set[str] | None = None,
):
    plugin_name = str(plugin.get("name") or "").strip().lower()
    internal_name = str(plugin.get("internalName") or "").strip().lower()
    if blacklist and (plugin_name in blacklist or internal_name in blacklist):
        return {"ok": False, "reason": "blacklisted"}

    status = plugin.get("status")
    try:
        status_num = int(status)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "invalid-status"}
    if status_num == 0:
        return {"ok": False, "reason": "status-down"}

    url = plugin.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return {"ok": False, "reason": "invalid-url"}

    expected_raw = plugin.get("fileHash")
    expected_norm = normalize_sha256(expected_raw)
    if expected_raw is not None and expected_norm is None:
        return {"ok": False, "reason": "invalid-file-hash"}
    cache_key = f"{url}|{expected_norm or ''}"
    if cache_lock:
        with cache_lock:
            if cache_key in cache:
                return cache[cache_key]
    else:
        if cache_key in cache:
            return cache[cache_key]

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12; CloudStream/4.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Accept": "application/octet-stream,*/*",
    }

    try:
        with requests.get(url, headers=headers, stream=True, timeout=(10, 35), allow_redirects=True) as r:
            if r.status_code != 200:
                result = {"ok": False, "reason": f"http-{r.status_code}"}
                _put_cache(cache, cache_key, result, cache_lock)
                return result

            content_length = r.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) <= 0:
                        result = {"ok": False, "reason": "empty-file"}
                        _put_cache(cache, cache_key, result, cache_lock)
                        return result
                    if int(content_length) > PLUGIN_MAX_BYTES:
                        result = {"ok": False, "reason": "file-too-large"}
                        _put_cache(cache, cache_key, result, cache_lock)
                        return result
                except ValueError:
                    pass

            chunks = []
            size = 0
            for chunk in r.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > PLUGIN_MAX_BYTES:
                    result = {"ok": False, "reason": "file-too-large"}
                    _put_cache(cache, cache_key, result, cache_lock)
                    return result
                chunks.append(chunk)

            if size == 0:
                result = {"ok": False, "reason": "empty-file"}
                _put_cache(cache, cache_key, result, cache_lock)
                return result

            content_bytes = b"".join(chunks)
            digest = hashlib.sha256(content_bytes)
            actual_hex = digest.hexdigest().lower()
            actual_hash = f"sha256-{actual_hex}"

            if expected_norm is not None:
                if actual_hex != expected_norm:
                    result = {
                        "ok": False,
                        "reason": "hash-mismatch",
                        "size": size,
                        "expected_hash": expected_raw,
                        "actual_hash": actual_hash,
                    }
                    _put_cache(cache, cache_key, result, cache_lock)
                    return result

            provider_check = "not-checked"
            provider_reason = None
            # Verify the required archive contents before checking candidate URLs.
            try:
                with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
                    names = z.namelist()
                    if "manifest.json" not in names or "classes.dex" not in names:
                        result = {"ok": False, "reason": "corrupted-cs3-missing-dex"}
                        _put_cache(cache, cache_key, result, cache_lock)
                        return result

                    if any(names.count(name) != 1 or z.getinfo(name).file_size > PLUGIN_MAX_BYTES
                           for name in ("manifest.json", "classes.dex")):
                        result = {"ok": False, "reason": "invalid-plugin-archive"}
                        _put_cache(cache, cache_key, result, cache_lock)
                        return result
                    try:
                        manifest = json.loads(z.read("manifest.json"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        manifest = None
                    if (not isinstance(manifest, dict)
                            or not isinstance(manifest.get("pluginClassName"), str)
                            or not manifest["pluginClassName"].strip()):
                        result = {"ok": False, "reason": "invalid-plugin-manifest"}
                        _put_cache(cache, cache_key, result, cache_lock)
                        return result
                    dex_bytes = z.read("classes.dex")
                    if not valid_dex_header(dex_bytes):
                        result = {"ok": False, "reason": "invalid-dex"}
                        _put_cache(cache, cache_key, result, cache_lock)
                        return result

                    if domain_cache is not None:
                        candidate_urls, dynamic_missing = provider_addresses.extract_provider_urls(
                            dex_bytes,
                            plugin.get("name") or "",
                            plugin.get("internalName") or "",
                            plugin.get("iconUrl") or "",
                        )
                        if dynamic_missing:
                            provider_check = "unverified"
                            provider_reason = "dynamic-domain-missing"
                        else:
                            dead, provider_reason = provider_checks.is_provider_dead(candidate_urls, domain_cache, domain_lock)
                            # Embedded URLs are candidates, not proof of the address used at runtime.
                            provider_check = "reachable" if candidate_urls and not dead else "unverified"
            except zipfile.BadZipFile:
                result = {"ok": False, "reason": "bad-zip-file"}
                _put_cache(cache, cache_key, result, cache_lock)
                return result

            result = {
                "ok": True,
                "reason": "ok",
                "size": size,
                "hash_checked": bool(expected_norm is not None),
                "actual_hash": actual_hash,
                "provider_check": provider_check,
                "provider_reason": provider_reason,
            }
            _put_cache(cache, cache_key, result, cache_lock)
            return result
    except requests.RequestException as e:
        result = {"ok": False, "reason": "request-error", "error": type(e).__name__}
        _put_cache(cache, cache_key, result, cache_lock)
        return result
