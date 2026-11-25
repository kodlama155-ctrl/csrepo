"""Check whether the provider sites found in a plugin can be reached."""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
from urllib.parse import urljoin, urlparse

import requests


PROVIDER_ERROR_MARKERS = (
    "500 | server error", "500 server error", "internal server error",
    "siteye ulaşılamıyor", "siteye ulasilamiyor",
    "error 520", "error 521", "error 522", "error 523", "error 524",
    "web server is down", "error 1020", "access denied", "attention required! | cloudflare",
)
PROVIDER_PARK_MARKERS = (
    "domain satılıktır", "domain satiliktir", "alan adı satılıktır", "satılık domain",
    "domain for sale", "domain is for sale", "this domain is for sale",
    "buy this domain", "parked domain", "domain parked", "is parked free",
    "contact the owner", "hugedomains", "afternic", "dan.com", "sedo.com",
    "domain has expired", "account suspended", "cgi-sys/defaultwebpage",
    "default website page",
)

# GitHub runners are outside Turkey; many Turkish sites answer them with 403/451 while
# the same page opens from Turkey. Only those answers are re-asked from a Turkish
# Globalping probe (anonymous, public API). Any missing answer keeps the rejection.
# Measurement results are public, so only the provider's own site address is sent.
TR_PROBE_ENABLED = os.getenv("CSREPO_TR_PROBE", "").strip() == "1"
TR_PROBE_STATUSES = (403, 451)
TR_PROBE_CHALLENGE_MARKERS = ("just a moment", "attention required", "cf-chl", "challenge-platform")
TR_PROBE_MAX_REDIRECTS = 2
TR_PROBE_MAX_MEASUREMENTS = 200
TR_PROBE_QUOTA_RESERVE = 10
TR_PROBE_POLLS = 30
TR_PROBE_POLL_SECONDS = 2.0
GLOBALPING_API = "https://api.globalping.io/v1"
GLOBALPING_HEADERS = {"User-Agent": "csrepo-cloudstream-finder (+https://github.com/kodlama155-ctrl/csrepo)"}
# Optional: a token raises the hourly allowance. It is only sent as a header, never logged.
GLOBALPING_TOKEN = os.getenv("GLOBALPING_TOKEN", "").strip()
_TR_PROBE = {"used": 0, "stopped": False, "warned": False, "token": bool(GLOBALPING_TOKEN)}
_TR_PROBE_LOCK = threading.Lock()


def _globalping_headers() -> dict:
    headers = dict(GLOBALPING_HEADERS)
    if _TR_PROBE["token"]:
        headers["Authorization"] = f"Bearer {GLOBALPING_TOKEN}"
    return headers


def _drop_globalping_token(status: int) -> bool:
    """A rejected (401) or spent (403/429) token degrades to the anonymous allowance once."""
    with _TR_PROBE_LOCK:
        if not _TR_PROBE["token"]:
            return False
        _TR_PROBE["token"] = False
    print(f"[tr] Globalping token kullanılamadı (HTTP {status}); tokensiz devam ediliyor")
    return True


def _stop_tr_probe(reason: str):
    with _TR_PROBE_LOCK:
        already = _TR_PROBE["stopped"]
        _TR_PROBE["stopped"] = True
    if not already:
        print(f"[tr] Globalping durduruldu: {reason}; kalan siteler eskisi gibi elenir")


def globalping_http(url: str):
    """One GET from a Turkish probe: {status, location, body} or None when unavailable."""
    if not TR_PROBE_ENABLED:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    with _TR_PROBE_LOCK:
        if _TR_PROBE["stopped"] or _TR_PROBE["used"] >= TR_PROBE_MAX_MEASUREMENTS:
            return None
        _TR_PROBE["used"] += 1
        first_measurement = _TR_PROBE["used"] == 1
    if first_measurement:
        # Presence only; the token value itself is never printed.
        print(f"[tr] Globalping başladı; token: {'var' if GLOBALPING_TOKEN else 'yok'}")
    request = {"method": "GET", "path": parsed.path or "/"}
    if parsed.query:
        request["query"] = parsed.query
    options = {"protocol": parsed.scheme.upper(), "request": request}
    if parsed.port:
        options["port"] = parsed.port
    body = {"type": "http", "target": parsed.hostname, "locations": [{"country": "TR"}],
            "limit": 1, "measurementOptions": options}
    try:
        created = requests.post(f"{GLOBALPING_API}/measurements", json=body,
                                headers=_globalping_headers(), timeout=25)
        if created.status_code in (401, 403, 429) and _drop_globalping_token(created.status_code):
            created = requests.post(f"{GLOBALPING_API}/measurements", json=body,
                                    headers=_globalping_headers(), timeout=25)
        if created.status_code in (403, 429):
            _stop_tr_probe(f"kota doldu (HTTP {created.status_code})")
            return None
        remaining = str(created.headers.get("x-ratelimit-remaining") or "")
        if remaining.isdigit() and int(remaining) <= TR_PROBE_QUOTA_RESERVE:
            _stop_tr_probe(f"kota azaldı ({remaining})")
        if created.status_code != 202:
            return None
        measurement = (created.json() or {}).get("id")
        if not measurement:
            return None
        for _ in range(TR_PROBE_POLLS):
            answer = requests.get(f"{GLOBALPING_API}/measurements/{measurement}",
                                  headers=_globalping_headers(), timeout=25)
            if answer.status_code != 200:
                return None
            doc = answer.json() or {}
            if doc.get("status") == "finished":
                for row in doc.get("results") or []:
                    result = row.get("result") or {}
                    status = result.get("statusCode")
                    if not isinstance(status, int):
                        continue
                    headers = {str(key).lower(): value for key, value in (result.get("headers") or {}).items()}
                    location = headers.get("location") or ""
                    if isinstance(location, list):
                        location = location[0] if location else ""
                    raw = result.get("rawBody")
                    return {"status": status, "location": str(location),
                            "body": (raw if isinstance(raw, str) else "")[:20000].lower()}
                return None
            time.sleep(TR_PROBE_POLL_SECONDS)
        return None
    except (requests.RequestException, ValueError, TypeError, AttributeError) as error:
        with _TR_PROBE_LOCK:
            first = not _TR_PROBE["warned"]
            _TR_PROBE["warned"] = True
        if first:
            print(f"[tr] Globalping cevap vermedi ({type(error).__name__}); site eskisi gibi elenir")
        return None


def opens_from_turkey(url: str) -> bool:
    """True only for a clean 200 page from Turkey, following at most two redirects."""
    current = url
    for _ in range(TR_PROBE_MAX_REDIRECTS + 1):
        answer = globalping_http(current)
        if not answer:
            return False
        if answer["status"] in (301, 302, 303, 307, 308) and answer["location"]:
            current = urljoin(current, answer["location"])
            if urlparse(current).scheme not in ("http", "https"):
                return False
            continue
        if answer["status"] != 200:
            return False
        body = answer["body"].lower()
        return not any(marker in body for marker in
                       (*PROVIDER_ERROR_MARKERS, *PROVIDER_PARK_MARKERS, *TR_PROBE_CHALLENGE_MARKERS))
    return False


def query_ttnet_dns(host: str) -> list[str]:
    """Query Türk Telekom DNS resolvers directly to catch BTK domain sinkholes globally."""
    ips = []
    for dns_ip in ("195.175.39.39", "195.175.39.40"):
        try:
            q = b"\xaa\xaa\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            for part in host.split("."):
                if not part:
                    continue
                q += bytes([len(part)]) + part.encode("ascii")
            q += b"\x00\x00\x01\x00\x01"
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.8)
            s.sendto(q, (dns_ip, 53))
            data, _ = s.recvfrom(512)
            s.close()
            idx = 12
            while idx < len(data) and data[idx] != 0:
                idx += data[idx] + 1
            idx += 5
            while idx < len(data):
                if data[idx] & 0xc0 == 0xc0:
                    idx += 2
                else:
                    while idx < len(data) and data[idx] != 0:
                        idx += data[idx] + 1
                    idx += 1
                if idx + 10 > len(data):
                    break
                rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", data[idx:idx+10])
                idx += 10
                if rtype == 1 and rdlen == 4 and idx + 4 <= len(data):
                    ips.append(socket.inet_ntoa(data[idx:idx+4]))
                idx += rdlen
            if ips:
                break
        except Exception:
            continue
    return ips


def is_provider_dead(urls: list[str], domain_cache: dict[str, tuple[bool, str]], lock: threading.Lock | None = None) -> tuple[bool, str]:
    if not urls:
        return False, "no-urls"
    reasons = []
    for u in urls:
        parsed = urlparse(u)
        host = parsed.hostname
        if not host:
            continue

        cached = None
        if lock:
            with lock:
                cached = domain_cache.get(u)
        else:
            cached = domain_cache.get(u)

        if cached is not None:
            dead, reason = cached
            if not dead:
                return False, reason
            reasons.append(reason)
            continue

        # 1. Query Türk Telekom DNS directly over UDP (catches BTK sinkholes even on GitHub Actions)
        try:
            tt_ips = query_ttnet_dns(host)
            is_btk = False
            for tip in tt_ips:
                if tip == "195.175.254.2" or tip.startswith("195.175.254.") or tip == "213.14.227.50" or tip.startswith("213.14.227."):
                    entry = (True, f"{host}:btk-blocked-sinkhole:{tip}")
                    if lock:
                        with lock:
                            domain_cache[u] = entry
                    else:
                        domain_cache[u] = entry
                    reasons.append(entry[1])
                    is_btk = True
                    break
            if is_btk:
                continue
        except Exception:
            pass

        # 2. Local DNS check
        try:
            resolved_ip = socket.gethostbyname(host)
            if resolved_ip == "195.175.254.2" or resolved_ip.startswith("195.175.254.") or resolved_ip == "213.14.227.50":
                entry = (True, f"{host}:btk-blocked-sinkhole")
                if lock:
                    with lock:
                        domain_cache[u] = entry
                else:
                    domain_cache[u] = entry
                reasons.append(entry[1])
                continue
        except Exception:
            entry = (True, f"{host}:dns-error")
            if lock:
                with lock:
                    domain_cache[u] = entry
            else:
                domain_cache[u] = entry
            reasons.append(entry[1])
            continue

        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 12; CloudStream/4.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            r = requests.get(u, headers=headers, timeout=4, allow_redirects=True)
            if r.status_code in TR_PROBE_STATUSES and opens_from_turkey(u):
                entry = (False, f"{host}:alive-tr")
                if lock:
                    with lock:
                        domain_cache[u] = entry
                else:
                    domain_cache[u] = entry
                return False, entry[1]
            if not 200 <= r.status_code < 300:
                entry = (True, f"{host}:http-{r.status_code}")
                if lock:
                    with lock:
                        domain_cache[u] = entry
                else:
                    domain_cache[u] = entry
                reasons.append(entry[1])
                continue

            body = r.text[:20000].lower()
            found_err = next((m for m in PROVIDER_ERROR_MARKERS if m in body), None)
            if found_err:
                entry = (True, f"{host}:error-page:{found_err}")
                if lock:
                    with lock:
                        domain_cache[u] = entry
                else:
                    domain_cache[u] = entry
                reasons.append(entry[1])
                continue

            found_park = next((m for m in PROVIDER_PARK_MARKERS if m in body), None)
            if found_park:
                entry = (True, f"{host}:parked-domain:{found_park}")
                if lock:
                    with lock:
                        domain_cache[u] = entry
                else:
                    domain_cache[u] = entry
                reasons.append(entry[1])
                continue

            entry = (False, f"{host}:alive")
            if lock:
                with lock:
                    domain_cache[u] = entry
            else:
                domain_cache[u] = entry
            return False, entry[1]
        except requests.exceptions.RequestException:
            entry = (True, f"{host}:unreachable")
            if lock:
                with lock:
                    domain_cache[u] = entry
            else:
                domain_cache[u] = entry
            reasons.append(entry[1])
            continue
        except Exception:
            entry = (True, f"{host}:unreachable-error")
            if lock:
                with lock:
                    domain_cache[u] = entry
            else:
                domain_cache[u] = entry
            reasons.append(entry[1])
            continue

    return True, "; ".join(reasons) if reasons else "all-urls-dead"
