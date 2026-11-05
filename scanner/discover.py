#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urljoin, urlparse
import io
import socket
import struct
import zipfile
import zlib
import concurrent.futures
import threading

import requests

API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
MAX_REPO_IDLE_DAYS = 365
REPO_WORKERS = 4

HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "csrepo-cloudstream-finder",
}

SEARCH_QUERIES = [
    "cloudstream Türkçe in:name,description",
    "cloudstream Turkish in:name,description",
    "cloudstream eklenti in:name,description",
    "Kekik cloudstream in:name,description",
    "cloudstream repo Türkçe in:name,description",
]

# Global code search is intentionally broad. Valid non-Turkish repositories found
# here are kept in data/global.json instead of polluting the Turkish feed.
CODE_QUERIES = [
    'filename:repo.json "pluginLists" "Türkçe"',
]
# Below 24 h so the daily search does not drift past delayed 6-hourly scans.
CODE_SEARCH_INTERVAL = timedelta(hours=20)

TR_LANGUAGE_VALUES = {
    "tr", "tr-tr", "tur", "turkish", "turkce", "türkçe", "turkiye", "türkiye"
}

# Strong Turkish ecosystem/content markers. Avoid generic words such as "film"
# by themselves because they are common in many languages.
TR_MARKERS = (
    "türk", "turk", "kekik", "eklenti", "sağlayıcı", "saglayici", "yayın",
    "yayin", "dizi", "izle", "belgesel", "çizgi", "cizgi", "inatbox",
    "rectv", "dizipal", "hdfilmcehennemi", "filmmakinesi", "turkanime",
    "sezonlukdizi", "sinewix", "sinema", "wiosinema", "wioanime",
    "wiodrama", "wioasya", "wiokids", "wiospor", "turkspor",
)

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_PLUGIN_LIST_CACHE = ContextVar("plugin_list_cache", default=None)
_HTTP_SESSION = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Code search allows 10 requests per minute; its window resets within ~60 s.
RATE_LIMIT_MAX_WAIT = 65


def rate_limit_wait(response_headers) -> int | None:
    """Seconds GitHub asks us to wait (retry-after, or reset when remaining is 0); None if not a rate limit."""
    retry_after = response_headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return int(retry_after)
    reset = response_headers.get("x-ratelimit-reset")
    if response_headers.get("x-ratelimit-remaining") == "0" and reset and reset.isdigit():
        return max(1, int(reset) - int(time.time()) + 1)
    return None


def rate_limit_summary(response_headers) -> str:
    fields = ("x-ratelimit-resource", "x-ratelimit-limit", "x-ratelimit-remaining", "retry-after")
    reset = response_headers.get("x-ratelimit-reset")
    reset_in = f" reset_in={int(reset) - int(time.time())}s" if reset and reset.isdigit() else ""
    return " ".join(f"{name.removeprefix('x-ratelimit-')}={response_headers.get(name)}" for name in fields) + reset_in


def api_get(path_or_url: str, params=None, allow_404=False):
    url = path_or_url if path_or_url.startswith("http") else f"{API}{path_or_url}"
    parsed_url = urlparse(url)
    headers = {}
    if TOKEN and parsed_url.scheme == "https" and parsed_url.netloc == "api.github.com":
        headers["Authorization"] = f"Bearer {TOKEN}"
    for attempt in range(3):
        r = getattr(_HTTP_SESSION, "value", SESSION).get(url, params=params, headers=headers, timeout=25)
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code in (403, 429) and attempt < 2:
            wait = rate_limit_wait(r.headers)
            if wait is None:
                wait = 2 + attempt * 3
            elif wait > RATE_LIMIT_MAX_WAIT:
                break  # Too long to wait inside a scan; report the limit below.
            time.sleep(wait)
            continue
        break
    if r.status_code in (403, 429) and rate_limit_wait(r.headers) is not None:
        print(f"[warn] GitHub rate limit: {rate_limit_summary(r.headers)}", flush=True)
    r.raise_for_status()
    return r.json()


def text_get(url: str):
    for attempt in range(3):
        r = getattr(_HTTP_SESSION, "value", SESSION).get(url, timeout=25)
        if r.status_code == 404:
            return None, None
        if r.status_code in (403, 429) and attempt < 2:
            time.sleep(2 + attempt * 3)
            continue
        r.raise_for_status()
        return r.text, r.headers.get("Last-Modified")
    return None, None


@contextmanager
def shared_plugin_lists():
    token = _PLUGIN_LIST_CACHE.set({"bodies": {}, "updates": {}, "locks": {}, "lock": threading.Lock()})
    try:
        yield
    finally:
        _PLUGIN_LIST_CACHE.reset(token)


@contextmanager
def plugin_list_cache_entry(section, key):
    cache = _PLUGIN_LIST_CACHE.get()
    if cache is None:
        yield None
        return
    with cache["lock"]:
        lock = cache["locks"].setdefault((section, key), threading.Lock())
    with lock:
        yield cache[section]


@contextmanager
def timed_phase(label: str):
    started = time.perf_counter()
    completed = False
    print(f"[phase] {label}", flush=True)
    try:
        yield
        completed = True
    finally:
        elapsed = time.perf_counter() - started
        status = "" if completed else " (tamamlanamadı)"
        print(f"[time] {label}: {elapsed:.1f} sn{status}", flush=True)


def plugin_list_cache_key(url: str):
    parsed_url = urlparse(url)
    if (parsed_url.scheme == "https" and parsed_url.netloc == "raw.githubusercontent.com"
            and all(key in {"t", "v"} for key, _ in parse_qsl(parsed_url.query, keep_blank_values=True))):
        parsed = parse_raw_github(url)
        if parsed:
            repo_full, ref, path = parsed
            return "github-raw", repo_full.lower(), ref, path
    return "url", url


def plugin_list_text(url: str):
    key = plugin_list_cache_key(url)
    with plugin_list_cache_entry("bodies", key) as cache:
        if cache is not None and key in cache:
            return cache[key]
        result = text_get(url)
        if cache is not None and result[0] is not None:
            cache[key] = result
        return result


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


IMPORTANT_REPOSITORIES = (
    "Wiojelt/TurkSinema",
    "Wiojelt/WioSinema",
    "feroxx/Kekik-cloudstream",
    "lepotane/MRC-builds",
    "Wiojelt/TurkSpor",
    "Emre-Kahveci/CloudStreamHub",
    "manitux-app/cs-plugins",
)

README_AUTO_START = "<!-- AUTO_REPOS_START -->"
README_AUTO_END = "<!-- AUTO_REPOS_END -->"


def extract_short_addresses(text: str) -> list[str]:
    """Extract source-published CloudStream short codes/URLs from README text."""
    found = []

    def add(value: str):
        value = value.strip().strip(chr(96) + "*_.,;:()[]{}<>")
        if value and value not in found:
            found.append(value)

    for value in re.findall(r"https?://(?:www\.)?py\.md/[A-Za-z0-9._~-]+", text, flags=re.I):
        add(value)
    for value in re.findall(r"https?://(?:www\.)?tinyurl\.com/[A-Za-z0-9._~/?=&%-]+", text, flags=re.I):
        add(value)
    for value in re.findall(r"(?<![\w])![A-Za-z0-9][A-Za-z0-9_-]{1,40}", text):
        add(value)

    label_re = re.compile(
        r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?(?:k[ıi]sa\s*kod|k[ıi]sakod)(?:\*\*)?\s*:\s*"
        + chr(96)
        + r"?(!?[A-Za-z0-9][A-Za-z0-9_-]{1,40})"
    )
    for match in label_re.finditer(text):
        add(match.group(1))

    return found


def readme_short_addresses(full_name: str, preferred_branches: list[str | None]) -> list[str]:
    owner, repo = full_name.split("/", 1)
    branches = []
    for branch in preferred_branches:
        if branch and branch not in branches:
            branches.append(branch)

    for branch in branches:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(branch, safe='')}/README.md"
        try:
            raw, _ = text_get(url)
        except requests.RequestException:
            raw = None
        if raw:
            return extract_short_addresses(raw)
    return []


def markdown_cell(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def build_main_readme(repos: list[dict], bundle: dict, generated_at: str, *, output_root: Path | None = None):
    by_repo = {item.get("repository"): item for item in repos}
    featured = [by_repo[x] for x in IMPORTANT_REPOSITORIES if x in by_repo]

    lines = [
        README_AUTO_START,
        "## 📦 Güncel Önemli Türkçe CloudStream Repoları",
        "",
        "Bu tablo bot tarafından otomatik güncellenir. Kısa kod/adresler kaynak repoların README dosyalarından tespit edilir.",
        "",
        f"**Son tarama:** <code>{generated_at}</code>",
        "",
        "| Repo | Durum | Eklenti | Kısa kod / adres | Uzun repo.json |",
        "|---|---|---:|---|---|",
    ]

    for item in featured:
        repo_name = markdown_cell(item.get("name") or item.get("repository"))
        repository_url = item.get("repository_url") or ""
        repo_label = f"[{repo_name}]({repository_url})" if repository_url else repo_name
        shorts = item.get("short_addresses") or []
        short_text = "<br>".join(f"<code>{markdown_cell(x)}</code>" for x in shorts) if shorts else "—"
        repo_url = markdown_cell(item.get("repo_url"))
        long_text = f"<code>{repo_url}</code>" if repo_url else "—"
        lines.append(
            f"| {repo_label} | {markdown_cell(item.get('status') or 'active')} | {int(item.get('plugin_count') or 0)} | {short_text} | {long_text} |"
        )

    lines.extend([
        "",
        "Tüm doğrulanmış depolar için [catalog.md](catalog.md) dosyasına bakın.",
        README_AUTO_END,
    ])
    auto_block = "\n".join(lines)

    readme_path = ROOT / "README.md"
    try:
        current = readme_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = "# csrepo\nCloudStream repo discovery and validation bot\n\n## Kısa Repo Adresleri\n\n- https://py.md/emirtv\n- !emirtv\n"

    if README_AUTO_START in current and README_AUTO_END in current:
        before = current.split(README_AUTO_START, 1)[0].rstrip()
        after = current.split(README_AUTO_END, 1)[1].lstrip()
        new_text = before + "\n\n" + auto_block
        if after:
            new_text += "\n\n" + after
    else:
        new_text = current.rstrip() + "\n\n" + auto_block

    target = (output_root / "README.md") if output_root is not None else readme_path
    target.write_text(new_text.rstrip() + "\n", encoding="utf-8")


def discover_candidates(probe_memory: dict | None = None,
                        search_memory: dict | None = None) -> dict[str, set[str]]:
    """probe_memory maps repository -> pushed_at of its last definite "no repo.json" probe.

    When given, it is read and replaced in place with this scan's results so unchanged
    repositories are not probed again; a new push always triggers a fresh probe.
    search_memory holds {"last_success": iso}; code search is skipped until it is
    CODE_SEARCH_INTERVAL old and updated in place after a fully successful search.
    """
    found: dict[str, set[str]] = {}
    previous_probes = dict(probe_memory or {})
    current_probes: dict[str, str] = {}
    self_repository = os.getenv("GITHUB_REPOSITORY", "kodlama155-ctrl/csrepo")

    def add(full_name: str, source: str):
        if "/" in full_name and full_name != self_repository:
            found.setdefault(full_name, set()).add(source)

    def looks_like_repo_manifest(raw: str | None) -> bool:
        if not raw:
            return False
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False
        return (
            isinstance(payload, dict)
            and isinstance(payload.get("name"), str)
            and bool(payload.get("name"))
            and isinstance(payload.get("pluginLists"), list)
            and bool(payload.get("pluginLists"))
        )

    def probe_manifest(full_name: str, default_branch: str | None) -> bool | None:
        """True: manifest found. False: every branch answered without one. None: a request failed."""
        owner, repo_name = full_name.split("/", 1)
        branches = []
        for branch in (default_branch, "builds", "main", "master"):
            if branch and branch not in branches:
                branches.append(branch)
        failed = False
        for branch in branches:
            url = (
                f"https://raw.githubusercontent.com/{owner}/{repo_name}/"
                f"{quote(branch, safe='')}/repo.json"
            )
            try:
                raw, _ = text_get(url)
            except requests.RequestException:
                failed = True
                continue
            if looks_like_repo_manifest(raw):
                return True
        return None if failed else False

    seeds = load_json(DATA / "seeds.json", [])
    for item in seeds:
        add(item["repository"], "seed")

    # Persistent registry: once a repository is discovered, keep checking it even
    # if GitHub Search is temporarily rate-limited or stops returning it.
    for full_name in load_json(DATA / "candidates.json", []):
        if isinstance(full_name, str):
            add(full_name, "registry")

    for query in SEARCH_QUERIES:
        try:
            result = api_get("/search/repositories", {"q": query, "per_page": 100, "page": 1})
            for item in result.get("items", []):
                if repo_is_stale(item.get("pushed_at")):
                    continue
                add(item["full_name"], f"repo-search:{query}")
        except requests.RequestException as e:
            print(f"[warn] repository search failed: {query}: {e}")

    # Code search is easily rate-limited; repositories it found stay in the registry,
    # so one successful search per CODE_SEARCH_INTERVAL is enough.
    last_code_search = parse_iso((search_memory or {}).get("last_success"))
    if search_memory is not None and datetime.now(timezone.utc) - last_code_search < CODE_SEARCH_INTERVAL:
        print(f"[info] code search skipped; last success {search_memory['last_success']}")
    else:
        code_search_ok = bool(CODE_QUERIES)
        for query in CODE_QUERIES:
            try:
                result = api_get("/search/code", {"q": query, "per_page": 100, "page": 1})
                for item in result.get("items", []):
                    add(item["repository"]["full_name"], f"code-search:{query}")
                time.sleep(1)
            except requests.RequestException as e:
                code_search_ok = False
                print(f"[warn] code search failed: {query}: {e}")
        if search_memory is not None and code_search_ok:
            search_memory["last_success"] = now_iso()

    # Search sibling repositories without trusting their names/descriptions.
    # Owners are learned from seeds and previously verified Turkish repos.
    known_repositories = {
        x.get("repository")
        for x in seeds
        if isinstance(x, dict) and isinstance(x.get("repository"), str)
    }
    for data_file in ("repos.json", "turkish_all.json"):
        for item in load_json(DATA / data_file, []):
            full_name = item.get("repository") if isinstance(item, dict) else None
            if isinstance(full_name, str) and "/" in full_name:
                known_repositories.add(full_name)

    known_owners = sorted({
        full_name.split("/", 1)[0]
        for full_name in known_repositories
        if isinstance(full_name, str) and "/" in full_name
    })

    for owner in known_owners:
        try:
            page = 1
            while page <= 3:
                repos = api_get(f"/users/{quote(owner)}/repos", {
                    "per_page": 100, "page": page, "sort": "pushed", "direction": "desc"
                })
                if not repos:
                    break
                for item in repos:
                    full_name = item.get("full_name")
                    if not isinstance(full_name, str) or full_name == self_repository:
                        continue
                    if repo_is_stale(item.get("pushed_at")):
                        continue

                    name = item.get("name", "")
                    desc = item.get("description") or ""
                    haystack = f"{name} {desc}".lower()

                    # Fast path for obviously named CloudStream projects.
                    if any(k in haystack for k in ("cloudstream", "wio", "kekik", "cs-plugin", "csrepo")):
                        add(full_name, f"owner-expand:{owner}")
                        continue

                    # Generic names such as "emir" are detected by their actual
                    # repo.json structure instead of repository metadata.
                    if full_name in found:
                        continue
                    pushed_at = item.get("pushed_at")
                    if isinstance(pushed_at, str) and previous_probes.get(full_name) == pushed_at:
                        # Not pushed since the last definite negative probe.
                        current_probes[full_name] = pushed_at
                        continue
                    probe = probe_manifest(full_name, item.get("default_branch"))
                    if probe:
                        add(full_name, f"owner-manifest:{owner}")
                    elif probe is False and isinstance(pushed_at, str):
                        current_probes[full_name] = pushed_at

                if len(repos) < 100:
                    break
                page += 1
        except requests.RequestException as e:
            print(f"[warn] owner expansion failed: {owner}: {e}")
            # Keep memory for owners that could not be listed this time.
            current_probes.update({name: pushed for name, pushed in previous_probes.items()
                                   if name.split("/", 1)[0] == owner})

    if probe_memory is not None:
        probe_memory.clear()
        probe_memory.update(sorted(current_probes.items()))
    return found


def branch_names(full_name: str, default_branch: str) -> list[str]:
    owner, repo = full_name.split("/", 1)
    out = []
    for preferred in ("builds", default_branch, "main", "master"):
        if preferred and preferred not in out:
            out.append(preferred)
    try:
        branches = api_get(f"/repos/{owner}/{repo}/branches", {"per_page": 100})
        for item in branches or []:
            name = item.get("name")
            if name and name not in out:
                out.append(name)
    except requests.RequestException:
        pass
    return out


def latest_commit_for_path(full_name: str, ref: str, path: str):
    owner, repo = full_name.split("/", 1)
    try:
        items = api_get(
            f"/repos/{owner}/{repo}/commits",
            {"sha": ref, "path": path, "per_page": 1, "page": 1},
            allow_404=True,
        )
        if not items:
            return None
        c = items[0].get("commit", {})
        return (c.get("committer") or {}).get("date") or (c.get("author") or {}).get("date")
    except requests.RequestException:
        return None


def parse_raw_github(url: str):
    try:
        p = urlparse(url)
        parts = [x for x in p.path.split("/") if x]
        if p.netloc == "github.com":
            if len(parts) < 5 or parts[2] != "raw":
                return None
            parts = parts[:2] + parts[3:]
        elif p.netloc == "cdn.jsdelivr.net":
            if len(parts) < 4 or parts[0] != "gh":
                return None
            repo, separator, ref = parts[2].partition("@")
            if not separator or not repo or not ref:
                return None
            parts = [parts[1], repo, ref, *parts[3:]]
        elif p.netloc != "raw.githubusercontent.com":
            return None
        if len(parts) < 4:
            return None
        owner, repo = parts[0], parts[1]
        rest = parts[2:]
        if len(rest) >= 4 and rest[0] == "refs" and rest[1] == "heads":
            ref = rest[2]
            file_path = "/".join(rest[3:])
        else:
            ref = rest[0]
            file_path = "/".join(rest[1:])
        return f"{owner}/{repo}", ref, file_path
    except Exception:
        return None


def validate_manifest(full_name: str, branch: str):
    owner, repo = full_name.split("/", 1)
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(branch, safe='')}/repo.json"
    raw, _ = text_get(raw_url)
    if not raw:
        return None
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(manifest, dict):
        return None
    if not manifest.get("name"):
        return None
    if not isinstance(manifest.get("pluginLists"), list) or not manifest["pluginLists"]:
        return None
    mv = manifest.get("manifestVersion", 1)
    if not isinstance(mv, int) or mv <= 0:
        return None
    return raw_url, manifest


def normalize_language(value) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("_", "-")
    return v or None


def plugin_list_update_info(url: str):
    parsed = parse_raw_github(url)
    if not parsed:
        return None, None
    repo_full, ref, path = parsed
    key = repo_full.lower(), ref, path
    with plugin_list_cache_entry("updates", key) as cache:
        if cache is not None and key in cache:
            return cache[key], repo_full
        updated_at = latest_commit_for_path(repo_full, ref, path)
        if cache is not None and updated_at is not None:
            cache[key] = updated_at
        return updated_at, repo_full


def plugin_list_info(url: str, update_info=None):
    raw, _ = plugin_list_text(url)
    if raw is None:
        return {
            "url": url, "reachable": False, "count": 0, "updated_at": None,
            "languages": {}, "turkish_language_count": 0, "text_sample": "",
            "declared_down_count": 0, "declared_status_count": 0,
        }
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "url": url, "reachable": False, "count": 0, "updated_at": None,
            "languages": {}, "turkish_language_count": 0, "text_sample": "",
            "declared_down_count": 0, "declared_status_count": 0,
        }

    if isinstance(payload, list):
        plugins = payload
    elif isinstance(payload, dict) and isinstance(payload.get("plugins"), list):
        plugins = payload["plugins"]
    else:
        plugins = []

    languages = Counter()
    text_bits = []
    declared_down_count = 0
    declared_status_count = 0
    for p in plugins:
        if not isinstance(p, dict):
            continue
        lang = normalize_language(p.get("language"))
        if lang:
            languages[lang] += 1
        try:
            plugin_status = int(p.get("status"))
            declared_status_count += 1
            if plugin_status == 0:
                declared_down_count += 1
        except (TypeError, ValueError):
            pass
        for field in ("name", "internalName", "description"):
            value = p.get(field)
            if isinstance(value, str):
                text_bits.append(value)

    updated_at, source_repository = update_info if update_info is not None else plugin_list_update_info(url)

    tr_count = sum(count for lang, count in languages.items() if lang in TR_LANGUAGE_VALUES)
    return {
        "url": url,
        "reachable": True,
        "count": len(plugins),
        "updated_at": updated_at,
        "source_repository": source_repository,
        "languages": dict(sorted(languages.items())),
        "turkish_language_count": tr_count,
        "declared_down_count": declared_down_count,
        "declared_status_count": declared_status_count,
        "text_sample": " ".join(text_bits[:80])[:5000],
    }


def turkish_classification(item: dict, discovery_sources: set[str], seed_repos: set[str], seed_owners: set[str]):
    reasons = []
    score = 0
    full_name = item.get("repository") or ""
    owner = full_name.split("/", 1)[0] if "/" in full_name else ""

    plugin_count = int(item.get("plugin_count") or 0)
    tr_lang_count = sum(
        int(x.get("turkish_language_count") or 0)
        for x in item.get("plugin_lists", [])
    )
    language_total = sum(
        sum(int(v or 0) for v in (x.get("languages") or {}).values())
        for x in item.get("plugin_lists", [])
    )
    tr_share = (tr_lang_count / language_total) if language_total else 0.0

    repo_text = " ".join([
        item.get("name") or "",
        item.get("description") or "",
        item.get("repository") or "",
    ]).lower()

    explicit_repo_markers = (
        "türk", "turk", "türkiye", "turkiye", "kekik",
        "sinetech.tr", "türkçe", "turkce",
    )
    explicit_matches = sorted({m for m in explicit_repo_markers if m in repo_text})

    source_repos = set(item.get("plugin_source_repositories") or [])
    turkish_source = any(
        ("kekik" in x.lower()) or ("wio" in x.lower()) or ("turk" in x.lower())
        for x in source_repos
    )

    # Scores are kept for sorting/explanation, but inclusion uses the stricter
    # evidence gate below. This prevents global mixed repos with a few TR
    # plugins from entering the app feed.
    if full_name in seed_repos:
        score += 100
        reasons.append("trusted-seed")

    if owner in seed_owners:
        score += 35
        reasons.append("trusted-turkish-owner")

    if tr_lang_count:
        score += min(100, 60 + tr_lang_count)
        reasons.append(f"plugins-language-tr:{tr_lang_count}/{language_total or plugin_count}")

    if explicit_matches:
        score += 60
        reasons.append("explicit-turkish-repo:" + ",".join(explicit_matches[:6]))

    text_parts = [repo_text]
    for p in item.get("plugin_lists", []):
        text_parts.append(p.get("text_sample") or "")
    haystack = " ".join(text_parts).lower()
    matched = sorted({marker for marker in TR_MARKERS if marker in haystack})
    if matched:
        score += min(50, 10 + len(matched) * 5)
        reasons.append("turkish-content-markers:" + ",".join(matched[:8]))

    if turkish_source:
        score += 35
        reasons.append("turkish-plugin-source")

    search_hit = any(
        source.startswith("repo-search:") and
        any(k in source.lower() for k in ("türkçe", "turkish", "eklenti", "kekik"))
        for source in discovery_sources
    )
    if search_hit:
        score += 10
        reasons.append("turkish-search-hit")

    strong_language = tr_lang_count > 0 and tr_share >= 0.50
    explicit_repo = bool(explicit_matches)
    trusted = full_name in seed_repos or owner in seed_owners

    is_turkish = trusted or strong_language or explicit_repo or turkish_source

    # Explicitly keep broad multi-language repos out unless there is another
    # strong Turkish-repository signal.
    if language_total and tr_lang_count and tr_share < 0.20 and not (trusted or explicit_repo or turkish_source):
        is_turkish = False
        reasons.append(f"excluded-low-tr-share:{tr_share:.2f}")

    return {
        "is_turkish": is_turkish,
        "score": score,
        "tr_language_count": tr_lang_count,
        "language_total": language_total,
        "tr_share": round(tr_share, 4),
        "reasons": reasons,
    }


def inspect_repo(full_name: str, discovery_sources: set[str]):
    owner, repo = full_name.split("/", 1)
    try:
        meta = api_get(f"/repos/{owner}/{repo}", allow_404=True)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 451:
            print(f"[skip] {full_name}: GitHub repository access blocked (HTTP 451)")
            return None
        raise
    if not meta or meta.get("private"):
        return None
    if repo_is_stale(meta.get("pushed_at")):
        return {
            "repository": full_name,
            "status": "stale",
            "skip_reason": "repository-not-updated",
            "last_repo_push": meta["pushed_at"],
            "checked_at": now_iso(),
        }

    manifest_hit = None
    manifest_error = None
    for branch in branch_names(full_name, meta.get("default_branch") or "main"):
        try:
            hit = validate_manifest(full_name, branch)
        except requests.RequestException as error:
            manifest_error = error
            hit = None
        if hit:
            manifest_hit = (branch, *hit)
            break

    if not manifest_hit:
        if manifest_error is not None:
            raise manifest_error
        return None

    branch, repo_json_url, manifest = manifest_hit
    list_updates = {}
    for url in manifest.get("pluginLists", []):
        if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in list_updates:
            list_updates[url] = plugin_list_update_info(url)
    if list_updates and all(repo_is_stale(info[0]) for info in list_updates.values()):
        return {
            "repository": full_name,
            "status": "stale",
            "skip_reason": "plugin-lists-not-updated",
            "last_repo_push": meta.get("pushed_at"),
            "last_plugins_update": max(info[0] for info in list_updates.values()),
            "checked_at": now_iso(),
        }

    plugin_infos = []
    for url in manifest.get("pluginLists", []):
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            plugin_infos.append({
                "url": str(url), "reachable": False, "count": 0, "updated_at": None,
                "languages": {}, "turkish_language_count": 0, "text_sample": "",
            "declared_down_count": 0, "declared_status_count": 0,
            })
            continue
        try:
            plugin_infos.append(plugin_list_info(url, list_updates[url]))
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code != 403:
                raise
            plugin_infos.append({
                "url": url, "reachable": False, "count": 0, "updated_at": None,
                "languages": {}, "turkish_language_count": 0, "text_sample": "",
                "declared_down_count": 0, "declared_status_count": 0,
                "check_error": "http-403",
            })

    plugin_count = sum(x.get("count", 0) for x in plugin_infos)
    declared_down_count = sum(int(x.get("declared_down_count") or 0) for x in plugin_infos)
    declared_status_count = sum(int(x.get("declared_status_count") or 0) for x in plugin_infos)
    reachable = bool(plugin_infos) and all(x.get("reachable") for x in plugin_infos)
    manifest_updated = latest_commit_for_path(full_name, branch, "repo.json")
    plugin_dates = [x.get("updated_at") for x in plugin_infos if x.get("updated_at")]
    plugins_updated = max(plugin_dates) if plugin_dates else None
    source_repos = sorted({x.get("source_repository") for x in plugin_infos if x.get("source_repository")})
    self_contained = bool(source_repos) and all(x == full_name for x in source_repos)

    if meta.get("archived"):
        status = "archived"
    elif any(x.get("check_error") for x in plugin_infos):
        status = "access-blocked"
    elif not reachable:
        status = "broken"
    elif plugin_count == 0:
        status = "empty"
    else:
        status = "active"

    return {
        "name": manifest.get("name"),
        "description": manifest.get("description") or meta.get("description"),
        "icon_url": manifest.get("iconUrl"),
        "repository": full_name,
        "repository_url": meta.get("html_url"),
        "repo_url": repo_json_url,
        "manifest_branch": branch,
        "manifest_version": manifest.get("manifestVersion", 1),
        "plugin_lists": plugin_infos,
        "plugin_count": plugin_count,
        "declared_down_count": declared_down_count,
        "declared_status_count": declared_status_count,
        "github_stars": int(meta.get("stargazers_count") or 0),
        "github_forks": int(meta.get("forks_count") or 0),
        "status": status,
        "archived": bool(meta.get("archived")),
        "fork": bool(meta.get("fork")),
        "parent": (meta.get("parent") or {}).get("full_name"),
        "self_contained": self_contained,
        "plugin_source_repositories": source_repos,
        "last_repo_push": meta.get("pushed_at"),
        "last_manifest_update": manifest_updated,
        "last_plugins_update": plugins_updated,
        "discovered_by": sorted(discovery_sources),
        "checked_at": now_iso(),
    }


def inspect_repositories(candidates: dict[str, set[str]]):
    """Inspect a few repositories concurrently; return results in stable order."""
    if not candidates:
        return
    sessions = []
    session_lock = threading.Lock()

    def initialize_worker():
        session = requests.Session()
        session.headers.update(HEADERS)
        _HTTP_SESSION.value = session
        with session_lock:
            sessions.append(session)

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(REPO_WORKERS, len(candidates)), initializer=initialize_worker,
        ) as executor:
            tasks = [
                (name, executor.submit(copy_context().run, inspect_repo, name, sources))
                for name, sources in sorted(candidates.items())
            ]
            for name, future in tasks:
                try:
                    item = future.result()
                except Exception as error:
                    yield name, None, error
                else:
                    yield name, item, None
    finally:
        for session in sessions:
            session.close()


def summarize_changes(old: list[dict], new: list[dict]):
    old_by = {x.get("repository"): x for x in old}
    new_by = {x.get("repository"): x for x in new}
    changes = []

    for key in sorted(new_by.keys() - old_by.keys()):
        changes.append({"type": "new", "repository": key, "plugin_count": new_by[key].get("plugin_count")})
    for key in sorted(old_by.keys() - new_by.keys()):
        changes.append({"type": "missing", "repository": key})
    for key in sorted(new_by.keys() & old_by.keys()):
        a, b = old_by[key], new_by[key]
        if a.get("plugin_count") != b.get("plugin_count"):
            changes.append({
                "type": "plugin_count",
                "repository": key,
                "old": a.get("plugin_count"),
                "new": b.get("plugin_count"),
            })
        if a.get("status") != b.get("status"):
            changes.append({
                "type": "status",
                "repository": key,
                "old": a.get("status"),
                "new": b.get("status"),
            })
        if a.get("last_plugins_update") != b.get("last_plugins_update"):
            changes.append({
                "type": "plugins_updated",
                "repository": key,
                "old": a.get("last_plugins_update"),
                "new": b.get("last_plugins_update"),
            })
    return changes


def compact_for_output(item: dict):
    # text_sample is only needed internally for language classification.
    out = dict(item)
    cleaned = []
    for p in out.get("plugin_lists", []):
        q = dict(p)
        q.pop("text_sample", None)
        cleaned.append(q)
    out["plugin_lists"] = cleaned
    return out


def parse_iso(value: str | None):
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def repo_is_stale(last_push: str | None, checked_at: datetime | None = None) -> bool:
    if not isinstance(last_push, str):
        return False
    pushed_at = parse_iso(last_push)
    if pushed_at.year <= 1 or pushed_at.tzinfo is None:
        return False
    checked_at = checked_at or datetime.now(timezone.utc)
    return pushed_at <= checked_at - timedelta(days=MAX_REPO_IDLE_DAYS)


def calculate_repo_score(item: dict):
    """
    0-100 ranking score. This is a technical/reliability ordering aid, not a
    claim that one repository is objectively "better".
    """
    plugin_count = int(item.get("plugin_count") or 0)
    down_count = int(item.get("declared_down_count") or 0)

    # 30 pts: repository's own plugin metadata should not mark entries down.
    if plugin_count > 0:
        declared_health_ratio = max(0.0, min(1.0, 1.0 - (down_count / plugin_count)))
    else:
        declared_health_ratio = 0.0
    health_points = 30.0 * declared_health_ratio

    # 25 pts: recent maintenance. Use the freshest useful repository signal.
    dates = [
        parse_iso(item.get("last_plugins_update")),
        parse_iso(item.get("last_manifest_update")),
        parse_iso(item.get("last_repo_push")),
    ]
    freshest = max(dates)
    if freshest.year <= 1:
        freshness_days = None
        freshness_points = 0.0
    else:
        freshness_days = max(0.0, (datetime.now(timezone.utc) - freshest).total_seconds() / 86400)
        if freshness_days <= 7:
            freshness_points = 25.0
        elif freshness_days <= 30:
            freshness_points = 22.0
        elif freshness_days <= 90:
            freshness_points = 17.0
        elif freshness_days <= 180:
            freshness_points = 12.0
        elif freshness_days <= 365:
            freshness_points = 7.0
        else:
            freshness_points = 2.0

    # 15 pts: prefer repositories that host their own plugin list/artifacts.
    originality_points = 0.0
    if item.get("self_contained"):
        originality_points += 10.0
    if not item.get("fork"):
        originality_points += 5.0

    # 15 pts: modest GitHub popularity signal. Log scaling prevents stars from
    # dominating the technical signals.
    stars = max(0, int(item.get("github_stars") or 0))
    forks = max(0, int(item.get("github_forks") or 0))
    stars_points = min(10.0, 10.0 * math.log1p(stars) / math.log(101))
    forks_points = min(5.0, 5.0 * math.log1p(forks) / math.log(51))
    popularity_points = stars_points + forks_points

    # 10 pts: useful breadth, capped at 50 plugins so giant mirrors do not win.
    coverage_points = min(10.0, (plugin_count / 50.0) * 10.0)

    # 5 pts: Turkish focus inside this Turkish catalogue.
    tr_share = float(item.get("turkish", {}).get("tr_share") or 0.0)
    turkish_focus_points = 5.0 * max(0.0, min(1.0, tr_share))

    components = {
        "declared_health": round(health_points, 1),
        "freshness": round(freshness_points, 1),
        "originality": round(originality_points, 1),
        "github_popularity": round(popularity_points, 1),
        "coverage": round(coverage_points, 1),
        "turkish_focus": round(turkish_focus_points, 1),
    }
    score = round(sum(components.values()), 1)

    return {
        "score": score,
        "components": components,
        "metrics": {
            "declared_down_count": down_count,
            "plugin_count": plugin_count,
            "freshness_days": round(freshness_days, 1) if freshness_days is not None else None,
            "github_stars": stars,
            "github_forks": forks,
            "fork": bool(item.get("fork")),
            "self_contained": bool(item.get("self_contained")),
        },
    }


def fetch_plugin_entries(url: str):
    raw, _ = plugin_list_text(url)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("plugins"), list):
        return [x for x in payload["plugins"] if isinstance(x, dict)]
    return []


PLUGIN_MAX_BYTES = 25 * 1024 * 1024


def plugin_variant_rank(plugin: dict, repo_item: dict, seed_repos: set[str]):
    version = plugin.get("version")
    try:
        version_num = int(version)
    except (TypeError, ValueError):
        version_num = -1
    trusted = 1 if repo_item.get("repository") in seed_repos else 0
    fresh = parse_iso(repo_item.get("last_plugins_update")).timestamp()
    tr_score = int(repo_item.get("turkish", {}).get("score") or 0)
    return (version_num, fresh, trusted, tr_score)


def normalize_sha256(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.lower().startswith("sha256-"):
        cleaned = cleaned[7:]
    elif cleaned.lower().startswith("sha256:"):
        cleaned = cleaned[7:]
    if re.fullmatch(r"[a-fA-F0-9]{64}", cleaned):
        return cleaned.lower()
    return None


def _put_cache(cache: dict[str, dict], key: str, value: dict, lock: threading.Lock | None):
    if lock:
        with lock:
            cache[key] = value
    else:
        cache[key] = value


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


def load_blacklist() -> set[str]:
    p = DATA / "blacklist.json"
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
                        candidate_urls, dynamic_missing = extract_provider_urls(
                            dex_bytes,
                            plugin.get("name") or "",
                            plugin.get("internalName") or "",
                            plugin.get("iconUrl") or "",
                        )
                        if dynamic_missing:
                            provider_check = "unverified"
                            provider_reason = "dynamic-domain-missing"
                        else:
                            dead, provider_reason = is_provider_dead(candidate_urls, domain_cache, domain_lock)
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


# Provider check results that reflect a scanner limitation rather than a dead site.
# Plugins with only these results stay published, marked provider_check="unverified".
CHECKER_LIMIT_REASONS = frozenset({"dynamic-domain-missing", "no-urls"})


def build_cloudstream_bundle(app_ready: list[dict], seed_repos: set[str], *, output_root: Path | None = None):
    candidates: dict[str, list[tuple[dict, dict]]] = {}

    for repo_item in app_ready:
        explicit_repo = any(
            reason.startswith("explicit-turkish-repo:")
            for reason in (repo_item.get("turkish", {}).get("reasons") or [])
        )
        trusted_repo = repo_item.get("repository") in seed_repos

        for plist in repo_item.get("plugin_lists", []):
            url = plist.get("url")
            if not isinstance(url, str):
                continue
            entries = fetch_plugin_entries(url)
            if not entries and (plist.get("count") or 0) > 0:
                raise RuntimeError("Plugin list changed or could not be read; previous results retained.")

            for plugin in entries:
                lang = normalize_language(plugin.get("language"))
                if lang not in TR_LANGUAGE_VALUES:
                    if lang is not None or not (explicit_repo or trusted_repo):
                        continue

                raw_key = (
                    str(plugin.get("internalName") or "").strip()
                    or str(plugin.get("name") or "").strip()
                    or str(plugin.get("url") or "").strip()
                )
                if not raw_key:
                    continue

                norm_key = raw_key.lower()
                m = re.match(r"^0\s+([a-zA-Z].*)", norm_key)
                key = m.group(1).strip() if m else norm_key
                candidates.setdefault(key, []).append((plugin, repo_item))

    published = []
    health_rows = []
    file_cache: dict[str, dict] = {}
    cache_lock = threading.Lock()
    domain_cache: dict[str, tuple[bool, str]] = {}
    domain_lock = threading.Lock()
    blacklist = load_blacklist()
    rejection_counts = Counter()
    fallback_count = 0

    # Pre-validate in parallel, one variant rank per round: round N checks the N-th best
    # variant only for plugins with no publishable variant yet. This downloads the same
    # files the sequential selection below would, but in parallel; selection reads the cache.
    ranked_variants = {
        key: sorted(variants, key=lambda pair: plugin_variant_rank(pair[0], pair[1], seed_repos), reverse=True)
        for key, variants in candidates.items()
    }
    pending = [key for key, ranked in ranked_variants.items() if ranked]
    depth = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        while pending:
            batch = [key for key in pending if depth < len(ranked_variants[key])]
            futures = {
                key: executor.submit(
                    validate_plugin_file,
                    ranked_variants[key][depth][0],
                    file_cache,
                    cache_lock,
                    domain_cache,
                    domain_lock,
                    blacklist,
                )
                for key in batch
            }
            pending = []
            for key, future in futures.items():
                check = future.result()
                reason = check.get("reason") or ""
                if reason in ("request-error", "http-408", "http-425", "http-429") or reason.startswith("http-5"):
                    continue  # Selection below stops the scan on this result; do not fetch more.
                if not (check.get("ok") and check.get("provider_check") == "reachable"):
                    pending.append(key)
            depth += 1

    for key, variants in candidates.items():
        ranked = sorted(
            variants,
            key=lambda pair: plugin_variant_rank(pair[0], pair[1], seed_repos),
            reverse=True,
        )

        selected = None
        unverifiable = None
        attempts = []
        for variant_index, (plugin, source_repo) in enumerate(ranked):
            check = validate_plugin_file(
                plugin,
                file_cache,
                cache_lock,
                domain_cache,
                domain_lock,
                blacklist,
            )
            reason = check.get("reason") or ""
            if reason in ("request-error", "http-408", "http-425", "http-429") or reason.startswith("http-5"):
                raise RuntimeError("Plugin download could not be verified; previous results retained.")
            publishable = bool(check.get("ok")) and check.get("provider_check") == "reachable"
            if check.get("ok") and not publishable:
                reason = "provider-unverified"
            attempts.append({
                "repository": source_repo.get("repository"),
                "url": plugin.get("url"),
                "version": plugin.get("version"),
                "result": reason,
                "provider_check": check.get("provider_check", "not-checked"),
                "provider_reason": check.get("provider_reason"),
            })
            if publishable:
                selected = (plugin, source_repo, check, variant_index)
                break
            if (unverifiable is None and check.get("ok")
                    and check.get("provider_reason") in CHECKER_LIMIT_REASONS):
                unverifiable = (plugin, source_repo, check, variant_index)
            rejection_counts[reason or "unknown"] += 1

        if not selected and unverifiable:
            # File checks passed; only the scanner could not find a site address to test.
            selected = unverifiable

        if not selected:
            health_rows.append({
                "internal_name": key,
                "status": "rejected",
                "attempts": attempts,
            })
            continue

        plugin, source_repo, check, variant_index = selected
        if variant_index > 0:
            fallback_count += 1

        clean = dict(plugin)
        # Always inject verified actual hash and size calculated from downloaded bytes
        if check.get("actual_hash"):
            clean["fileHash"] = check["actual_hash"]
        if check.get("size"):
            clean["fileSize"] = check["size"]

        published.append(clean)
        health_rows.append({
            "internal_name": key,
            "name": plugin.get("name"),
            "status": "healthy",
            "repository": source_repo.get("repository"),
            "url": plugin.get("url"),
            "version": plugin.get("version"),
            "size": check.get("size"),
            "hash_checked": check.get("hash_checked", False),
            "provider_check": check.get("provider_check", "not-checked"),
            "provider_reason": check.get("provider_reason"),
            "fallback_used": variant_index > 0,
            "attempts": attempts if variant_index > 0 else None,
        })

    if not published:
        raise RuntimeError("No verified plugins; previous results retained.")

    published.sort(key=lambda x: (
        str(x.get("name") or x.get("internalName") or "").lower(),
        str(x.get("internalName") or "").lower(),
    ))
    health_rows.sort(key=lambda x: (
        x.get("status") != "healthy",
        str(x.get("name") or x.get("internal_name") or "").lower(),
    ))

    PRIVATE_MANIFEST = "repo-x92.json"
    PRIVATE_PLUGINS = "plugins-x92.json"

    repo_manifest = {
        "name": "EmirTV",
        "description": "Otomatik doğrulanan ve tekilleştirilen Türkçe CloudStream eklenti deposu.",
        "iconUrl": "https://raw.githubusercontent.com/kodlama155-ctrl/csrepo/main/assets/emirtv-icon.png",
        "manifestVersion": 1,
        # The list was scanned now; clients can tell a fresh list from a stale one.
        "generated_at": now_iso(),
        "pluginLists": [
            f"https://raw.githubusercontent.com/kodlama155-ctrl/csrepo/main/{PRIVATE_PLUGINS}"
        ],
    }

    legacy_manifest = {
        "name": "Kapatıldı",
        "description": "Bu depo adresi taşınmıştır.",
        "manifestVersion": 1,
        "pluginLists": [],
    }

    health = {
        "generated_at": now_iso(),
        "candidate_plugin_count": len(candidates),
        "healthy_plugin_count": len(published),
        "rejected_plugin_count": len(candidates) - len(published),
        "fallback_used_count": fallback_count,
        "provider_unverified_count": sum(row.get("provider_check") != "reachable" for row in health_rows if row["status"] == "healthy"),
        "rejection_attempt_counts": dict(sorted(rejection_counts.items())),
        "checks": {
            "status_zero_rejected": True,
            "http_200_required": True,
            "non_empty_required": True,
            "sha256_verified_when_provided": True,
            "dex_and_manifest_verified": True,
            "provider_domain_health_verified": False,
            "provider_checks_are_advisory": False,
            "provider_reachable_required": True,
            "provider_unverifiable_allowed": sorted(CHECKER_LIMIT_REASONS),
            "max_file_bytes": PLUGIN_MAX_BYTES,
            "executes_plugin_code": False,
        },
        "plugins": health_rows,
    }

    target_root = ROOT if output_root is None else output_root
    target_data = DATA if output_root is None else output_root / "data"
    save_json(target_root / PRIVATE_PLUGINS, published)
    live_published = [
        plugin for plugin in published
        if isinstance(plugin.get("tvTypes"), list) and "Live" in plugin["tvTypes"]
    ]
    save_json(target_data / "live-plugins.json", live_published)
    save_json(target_root / PRIVATE_MANIFEST, repo_manifest)
    save_json(target_root / "plugins.json", [])
    save_json(target_root / "repo.json", legacy_manifest)
    save_json(target_data / "plugin-health.json", health)
    return len(published), health["rejected_plugin_count"], fallback_count


def cloudstream_install_url(url: str) -> str:
    if url.startswith("https://"):
        return "cloudstreamrepo://" + url[len("https://"):]
    return url


def build_repo_catalog(app_ready: list[dict], published_plugin_count: int, seed_repos: set[str], *, output_root: Path | None = None):
    target_root = ROOT if output_root is None else output_root
    target_data = DATA if output_root is None else output_root / "data"
    bundle_url = "https://raw.githubusercontent.com/kodlama155-ctrl/csrepo/main/repo-x92.json"
    generated_at = now_iso()

    bundle = {
        "id": "emir-cloudstream-all",
        "type": "bundle",
        "name": "EmirTV — Hepsi Bir Arada",
        "description": "Aktif Türkçe CloudStream eklentilerinin otomatik doğrulanan ve tekilleştirilen birleşik deposu.",
        "repo_url": bundle_url,
        "install_url": cloudstream_install_url(bundle_url),
        "plugin_count": published_plugin_count,
        "source_repository_count": len(app_ready),
        "status": "active",
        "recommended": True,
    }

    repos = []
    for item in app_ready:
        repo_url = item.get("repo_url")
        if not isinstance(repo_url, str) or not repo_url.startswith(("https://", "http://")):
            continue
        repos.append({
            "id": item.get("repository"),
            "type": "repository",
            "name": item.get("name") or item.get("repository"),
            "description": item.get("description"),
            "icon_url": item.get("icon_url"),
            "repository": item.get("repository"),
            "repository_url": item.get("repository_url"),
            "repo_url": repo_url,
            "install_url": cloudstream_install_url(repo_url),
            "plugin_count": item.get("plugin_count") or 0,
            "repo_score": item.get("repo_score") or 0,
            "repo_score_details": item.get("repo_score_details") or {},
            "github_stars": item.get("github_stars") or 0,
            "github_forks": item.get("github_forks") or 0,
            "last_plugins_update": item.get("last_plugins_update"),
            "short_addresses": item.get("short_addresses") or [],
            "fork": bool(item.get("fork")),
            "parent": item.get("parent"),
            "self_contained": bool(item.get("self_contained")),
            "trusted_seed": item.get("repository") in seed_repos,
            "turkish_score": item.get("turkish", {}).get("score") or 0,
            "status": item.get("status"),
        })

    repos.sort(key=lambda x: (
        -(x.get("repo_score") or 0),
        not x.get("trusted_seed", False),
        -(x.get("plugin_count") or 0),
        str(x.get("name") or "").lower(),
    ))

    catalog = {
        "name": "EmirTV — Depoları Seç",
        "description": "Hepsi Bir Arada deposunu veya istediğiniz Türkçe CloudStream depolarını tek tek seçebilirsiniz.",
        "generated_at": generated_at,
        "sections": [
            {
                "id": "all",
                "title": "Hepsi Bir Arada",
                "items": [bundle],
            },
            {
                "id": "individual",
                "title": "Tek Tek Depolar",
                "items": repos,
            },
        ],
    }
    save_json(target_data / "catalog.json", catalog)

    # Simple official-style repository database: original repos.
    repo_urls = []
    seen = set()
    for item in repos:
        url = item["repo_url"]
        if url not in seen:
            seen.add(url)
            repo_urls.append(url)
    save_json(target_root / "repos-db.json", repo_urls)

    lines = [
        "# Türkçe CloudStream Depo Kataloğu",
        "",
        "Otomatik doğrulanan ve aktif Türkçe CloudStream eklenti depoları:",
        "",
        "## Doğrulanmış Kaynak Depolar",
        "",
    ]
    for item in repos:
        flags = []
        if item.get("trusted_seed"):
            flags.append("ana kaynak")
        if item.get("fork"):
            flags.append("fork")
        suffix = f" — RepoScore {item.get('repo_score', 0):.1f}/100 — {item['plugin_count']} eklenti"
        if flags:
            suffix += " — " + ", ".join(flags)
        lines.append(f"- [{item['name']}]({item['install_url']}){suffix}")
    lines.append("")
    (target_root / "catalog.md").write_text("\n".join(lines), encoding="utf-8")
    build_main_readme(repos, bundle, generated_at, output_root=output_root)

    return len(repos)


def inspect_candidates(candidates: dict[str, set[str]], old_tr: list[dict], seeds: list[dict]):
    previous_active_repos = {item.get("repository") for item in old_tr}
    seed_repos = {x["repository"] for x in seeds}
    seed_owners = {x.split("/", 1)[0] for x in seed_repos}
    turkish, global_other, stale_repositories, inspection_failures = [], [], [], []
    skipped = []
    for i, (full_name, item, error) in enumerate(inspect_repositories(candidates), 1):
        try:
            if error is not None:
                raise error
            if item and item.get("status") == "access-blocked" and full_name in previous_active_repos:
                raise RuntimeError("Previously active repository access denied; previous results retained.")
            if not item:
                print(f"[skip] {full_name}: no valid CloudStream repo.json")
                # Covers missing/private repositories, HTTP 451 and absent or invalid repo.json.
                skipped.append({"repository": full_name, "reason": "no-valid-repo-json",
                                "discovered_by": sorted(candidates[full_name])})
                continue
            if item.get("status") == "stale":
                stale_repositories.append(item)
                print(f"[skip] {full_name}: {item.get('skip_reason', 'stale')} ({MAX_REPO_IDLE_DAYS} days)")
                continue

            cls = turkish_classification(item, candidates[full_name], seed_repos, seed_owners)
            item["turkish"] = cls
            if cls["is_turkish"]:
                item["short_addresses"] = readme_short_addresses(
                    full_name,
                    [item.get("manifest_branch"), "main", "master", "builds"],
                )
            else:
                item["short_addresses"] = []
            ranking = calculate_repo_score(item)
            item["repo_score"] = ranking["score"]
            item["repo_score_details"] = ranking
            clean = compact_for_output(item)

            if cls["is_turkish"]:
                turkish.append(clean)
                print(f"[tr] {full_name}: {item['status']} / {item['plugin_count']} plugins / score={cls['score']}")
            else:
                global_other.append(clean)
                print(f"[global] {full_name}: {item['status']} / {item['plugin_count']} plugins")
        except Exception as e:
            inspection_failures.append(full_name)
            print(f"[warn] {full_name}: {type(e).__name__}: {e}")

        if i % 20 == 0:
            time.sleep(1)

    if inspection_failures:
        raise RuntimeError(f"Scan incomplete: {len(inspection_failures)} repositories could not be checked; previous results retained.")

    sort_key = lambda x: (
        x.get("status") != "active",
        -(x.get("repo_score") or 0),
        -(x.get("turkish", {}).get("score") or 0),
        x.get("name") or "",
        x.get("repository") or "",
    )
    turkish.sort(key=sort_key)
    global_other.sort(key=lambda x: (
        x.get("status") != "active",
        x.get("name") or "",
        x.get("repository") or "",
    ))

    # Application feed: only active, non-empty and reachable Turkish repos.
    app_ready = [x for x in turkish if x.get("status") == "active" and (x.get("plugin_count") or 0) > 0]
    if not app_ready:
        raise RuntimeError("No active repositories verified; previous results retained.")

    return {"turkish": turkish, "global_other": global_other,
            "stale_repositories": stale_repositories, "app_ready": app_ready, "skipped": skipped}


def write_scan_outputs(candidates, inspection, old_tr, seeds, bundle_counts, *, output_root=None,
                       owner_probes=None, code_search=None):
    target_data = DATA if output_root is None else output_root / "data"
    turkish = inspection["turkish"]
    global_other = inspection["global_other"]
    stale_repositories = inspection["stale_repositories"]
    app_ready = inspection["app_ready"]
    seed_repos = {x["repository"] for x in seeds}
    published_plugin_count, rejected_plugin_count, plugin_fallback_count = bundle_counts

    catalog_repo_count = build_repo_catalog(app_ready, published_plugin_count, seed_repos, output_root=output_root)
    live_plugin_count = len(load_json(target_data / "live-plugins.json", []))

    changes = summarize_changes(old_tr, app_ready)
    save_json(target_data / "candidates.json", sorted(candidates))
    # Repositories checked and found without repo.json, keyed to their last push.
    save_json(target_data / "owner-probes.json", dict(sorted((owner_probes or {}).items())))
    save_json(target_data / "code-search.json", code_search or {})
    save_json(target_data / "repos.json", app_ready)
    save_json(target_data / "ranking.json", [
        {
            "rank": i,
            "repository": x.get("repository"),
            "name": x.get("name"),
            "repo_score": x.get("repo_score"),
            "repo_score_details": x.get("repo_score_details"),
            "plugin_count": x.get("plugin_count"),
            "status": x.get("status"),
        }
        for i, x in enumerate(app_ready, 1)
    ])
    save_json(target_data / "turkish_all.json", turkish)
    save_json(target_data / "global.json", global_other)
    save_json(target_data / "changes.json", {
        "generated_at": now_iso(),
        "candidate_count": len(candidates),
        "stale_repo_count": len(stale_repositories),
        "stale_repositories": stale_repositories,
        "verified_count": len(turkish) + len(global_other),
        "turkish_count": len(turkish),
        "app_ready_count": len(app_ready),
        "published_plugin_count": published_plugin_count,
        "live_plugin_count": live_plugin_count,
        "rejected_plugin_count": rejected_plugin_count,
        "plugin_fallback_count": plugin_fallback_count,
        "catalog_repo_count": catalog_repo_count,
        "global_count": len(global_other),
        "changes": changes,
    })

    return catalog_repo_count


def stage_runner():
    try:
        from scanner import stages
    except ModuleNotFoundError as error:
        if error.name != "scanner":
            raise
        import stages
    return stages


def main():
    return stage_runner().run_stages(globals())


def cli(argv=None):
    return stage_runner().run_cli(globals(), argv)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    cli()
