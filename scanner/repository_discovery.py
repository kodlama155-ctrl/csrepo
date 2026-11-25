"""Find CloudStream repositories and read their manifests and plugin lists."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, urlparse

import requests

from . import scan_utils
from .scan_utils import load_json, now_iso, parse_iso


API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
MAX_REPO_IDLE_DAYS = 365

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

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_PLUGIN_LIST_CACHE = ContextVar("plugin_list_cache", default=None)
_HTTP_SESSION = threading.local()


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


def open_worker_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    _HTTP_SESSION.value = session
    return session


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


def repo_is_stale(last_push: str | None, checked_at: datetime | None = None) -> bool:
    if not isinstance(last_push, str):
        return False
    pushed_at = parse_iso(last_push)
    if pushed_at.year <= 1 or pushed_at.tzinfo is None:
        return False
    checked_at = checked_at or datetime.now(timezone.utc)
    return pushed_at <= checked_at - timedelta(days=MAX_REPO_IDLE_DAYS)


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

    seeds = load_json(scan_utils.DATA / "seeds.json", [])
    for item in seeds:
        add(item["repository"], "seed")

    # Persistent registry: once a repository is discovered, keep checking it even
    # if GitHub Search is temporarily rate-limited or stops returning it.
    for full_name in load_json(scan_utils.DATA / "candidates.json", []):
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
        for item in load_json(scan_utils.DATA / data_file, []):
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
