#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
import io
import socket
import zipfile
import concurrent.futures
import threading

import requests

API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "csrepo-cloudstream-finder",
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

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


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def api_get(path_or_url: str, params=None, allow_404=False):
    url = path_or_url if path_or_url.startswith("http") else f"{API}{path_or_url}"
    for attempt in range(3):
        r = SESSION.get(url, params=params, timeout=25)
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code in (403, 429) and attempt < 2:
            time.sleep(2 + attempt * 3)
            continue
        r.raise_for_status()
        return r.json()
    return None


def text_get(url: str):
    for attempt in range(3):
        r = SESSION.get(url, timeout=25)
        if r.status_code == 404:
            return None, None
        if r.status_code in (403, 429) and attempt < 2:
            time.sleep(2 + attempt * 3)
            continue
        r.raise_for_status()
        return r.text, r.headers.get("Last-Modified")
    return None, None


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


def build_main_readme(repos: list[dict], bundle: dict, generated_at: str):
    by_repo = {item.get("repository"): item for item in repos}
    featured = [by_repo[x] for x in IMPORTANT_REPOSITORIES if x in by_repo]

    lines = [
        README_AUTO_START,
        "## 📦 Güncel Önemli Türkçe CloudStream Repoları",
        "",
        "Bu tablo bot tarafından otomatik güncellenir. Kısa kod/adresler kaynak repoların README dosyalarından tespit edilir.",
        "",
        f"**Son tarama:** <code>{generated_at}</code>  ",
        f"**EmirTV birleşik depo:** <code>https://py.md/emirtv</code> · <code>!emirtv</code> · **{bundle.get('plugin_count', 0)} eklenti**",
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

    readme_path.write_text(new_text.rstrip() + "\n", encoding="utf-8")


def discover_candidates() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
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

    def probe_manifest(full_name: str, default_branch: str | None) -> bool:
        owner, repo_name = full_name.split("/", 1)
        branches = []
        for branch in (default_branch, "builds", "main", "master"):
            if branch and branch not in branches:
                branches.append(branch)
        for branch in branches:
            url = (
                f"https://raw.githubusercontent.com/{owner}/{repo_name}/"
                f"{quote(branch, safe='')}/repo.json"
            )
            try:
                raw, _ = text_get(url)
            except requests.RequestException:
                continue
            if looks_like_repo_manifest(raw):
                return True
        return False

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
                add(item["full_name"], f"repo-search:{query}")
        except requests.RequestException as e:
            print(f"[warn] repository search failed: {query}: {e}")

    for query in CODE_QUERIES:
        try:
            result = api_get("/search/code", {"q": query, "per_page": 100, "page": 1})
            for item in result.get("items", []):
                add(item["repository"]["full_name"], f"code-search:{query}")
            time.sleep(1)
        except requests.RequestException as e:
            print(f"[warn] code search failed: {query}: {e}")

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

                    name = item.get("name", "")
                    desc = item.get("description") or ""
                    haystack = f"{name} {desc}".lower()

                    # Fast path for obviously named CloudStream projects.
                    if any(k in haystack for k in ("cloudstream", "wio", "kekik", "cs-plugin", "csrepo")):
                        add(full_name, f"owner-expand:{owner}")
                        continue

                    # Generic names such as "emir" are detected by their actual
                    # repo.json structure instead of repository metadata.
                    if full_name not in found and probe_manifest(full_name, item.get("default_branch")):
                        add(full_name, f"owner-manifest:{owner}")

                if len(repos) < 100:
                    break
                page += 1
        except requests.RequestException as e:
            print(f"[warn] owner expansion failed: {owner}: {e}")

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
        if p.netloc != "raw.githubusercontent.com":
            return None
        parts = [x for x in p.path.split("/") if x]
        if len(parts) < 4:
            return None
        owner, repo = parts[0], parts[1]
        rest = parts[2:]
        if len(rest) >= 5 and rest[0] == "refs" and rest[1] == "heads":
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


def plugin_list_info(url: str):
    raw, _ = text_get(url)
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

    updated_at = None
    parsed = parse_raw_github(url)
    if parsed:
        repo_full, ref, path = parsed
        updated_at = latest_commit_for_path(repo_full, ref, path)

    tr_count = sum(count for lang, count in languages.items() if lang in TR_LANGUAGE_VALUES)
    return {
        "url": url,
        "reachable": True,
        "count": len(plugins),
        "updated_at": updated_at,
        "source_repository": parsed[0] if parsed else None,
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
    meta = api_get(f"/repos/{owner}/{repo}", allow_404=True)
    if not meta or meta.get("private"):
        return None

    manifest_hit = None
    for branch in branch_names(full_name, meta.get("default_branch") or "main"):
        try:
            hit = validate_manifest(full_name, branch)
        except requests.RequestException:
            hit = None
        if hit:
            manifest_hit = (branch, *hit)
            break

    if not manifest_hit:
        return None

    branch, repo_json_url, manifest = manifest_hit
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
            plugin_infos.append(plugin_list_info(url))
        except requests.RequestException:
            plugin_infos.append({
                "url": url, "reachable": False, "count": 0, "updated_at": None,
                "languages": {}, "turkish_language_count": 0, "text_sample": "",
            "declared_down_count": 0, "declared_status_count": 0,
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
    raw, _ = text_get(url)
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
    "jsdelivr.net", "wikimedia.org"
}


def extract_provider_urls(dex_bytes: bytes, plugin_name: str) -> list[str]:
    raw_urls = re.findall(rb'https?://[a-zA-Z0-9\.\-_]+(?::\d+)?(?:/[a-zA-Z0-9\.\-_]*)*', dex_bytes)
    cleaned = set()
    for u in raw_urls:
        try:
            s = u.decode("ascii", errors="ignore").strip().rstrip("/")
            domain = s.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
            if any(domain == ign or domain.endswith("." + ign) for ign in IGNORED_PROVIDER_DOMAINS):
                continue
            if "." in domain and len(domain) > 4:
                cleaned.add("https://" + domain)
        except Exception:
            pass
    norm_name = re.sub(r"[^a-zA-Z0-9]", "", plugin_name.lower())
    matched = [u for u in cleaned if norm_name and norm_name in u.lower()]
    return matched if matched else list(cleaned)[:3]


def is_provider_dead(urls: list[str], domain_cache: dict[str, tuple[bool, str]], lock: threading.Lock | None = None) -> tuple[bool, str]:
    if not urls:
        return False, "no-urls"
    reasons = []
    for u in urls:
        parsed = urlparse(u)
        host = parsed.netloc.split(":")[0]
        if not host:
            continue

        cached = None
        if lock:
            with lock:
                cached = domain_cache.get(host)
        else:
            cached = domain_cache.get(host)

        if cached is not None:
            dead, reason = cached
            if not dead:
                return False, reason
            reasons.append(reason)
            continue

        try:
            resolved_ip = socket.gethostbyname(host)
            if resolved_ip == "195.175.254.2" or resolved_ip.startswith("195.175.254."):
                entry = (True, f"{host}:btk-blocked-sinkhole")
                if lock:
                    with lock:
                        domain_cache[host] = entry
                else:
                    domain_cache[host] = entry
                reasons.append(entry[1])
                continue
        except Exception:
            entry = (True, f"{host}:dns-error")
            if lock:
                with lock:
                    domain_cache[host] = entry
            else:
                domain_cache[host] = entry
            reasons.append(entry[1])
            continue

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            r = requests.get(u, headers=headers, timeout=4, allow_redirects=True)
            if r.status_code in (404, 410, 502, 504):
                entry = (True, f"{host}:http-{r.status_code}")
                if lock:
                    with lock:
                        domain_cache[host] = entry
                else:
                    domain_cache[host] = entry
                reasons.append(entry[1])
                continue

            body = r.text[:20000].lower()
            park_markers = [
                "domain satılıktır", "domain satiliktir", "alan adı satılıktır", "satılık domain",
                "domain for sale", "domain is for sale", "this domain is for sale",
                "buy this domain", "parked domain", "domain parked", "is parked free",
                "contact the owner", "hugedomains", "afternic", "dan.com", "sedo.com",
                "domain has expired", "account suspended", "cgi-sys/defaultwebpage",
                "default website page"
            ]
            found_park = next((m for m in park_markers if m in body), None)
            if found_park:
                entry = (True, f"{host}:parked-domain:{found_park}")
                if lock:
                    with lock:
                        domain_cache[host] = entry
                else:
                    domain_cache[host] = entry
                reasons.append(entry[1])
                continue

            entry = (False, f"{host}:alive")
            if lock:
                with lock:
                    domain_cache[host] = entry
            else:
                domain_cache[host] = entry
            return False, entry[1]
        except requests.exceptions.RequestException:
            entry = (True, f"{host}:unreachable")
            if lock:
                with lock:
                    domain_cache[host] = entry
            else:
                domain_cache[host] = entry
            reasons.append(entry[1])
            continue
        except Exception:
            entry = (True, f"{host}:unreachable-error")
            if lock:
                with lock:
                    domain_cache[host] = entry
            else:
                domain_cache[host] = entry
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

            # Deep verification: Valid ZIP with manifest.json and classes.dex
            try:
                with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
                    names = z.namelist()
                    if "manifest.json" not in names or "classes.dex" not in names:
                        result = {"ok": False, "reason": "corrupted-cs3-missing-dex"}
                        _put_cache(cache, cache_key, result, cache_lock)
                        return result

                    if domain_cache is not None:
                        dex_bytes = z.read("classes.dex")
                        candidate_urls = extract_provider_urls(dex_bytes, plugin.get("name") or "")
                        dead, dead_reason = is_provider_dead(candidate_urls, domain_cache, domain_lock)
                        if dead:
                            result = {
                                "ok": False,
                                "reason": f"provider-dead:{dead_reason}",
                                "dead_urls": candidate_urls,
                                "size": size,
                                "actual_hash": actual_hash,
                            }
                            _put_cache(cache, cache_key, result, cache_lock)
                            return result
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
            }
            _put_cache(cache, cache_key, result, cache_lock)
            return result
    except requests.RequestException as e:
        result = {"ok": False, "reason": "request-error", "error": type(e).__name__}
        _put_cache(cache, cache_key, result, cache_lock)
        return result


def build_cloudstream_bundle(app_ready: list[dict], seed_repos: set[str]):
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
            try:
                entries = fetch_plugin_entries(url)
            except requests.RequestException:
                continue

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

    # Pre-validate top candidate of each plugin in parallel
    top_candidates = []
    for variants in candidates.values():
        ranked = sorted(
            variants,
            key=lambda pair: plugin_variant_rank(pair[0], pair[1], seed_repos),
            reverse=True,
        )
        if ranked:
            top_candidates.append(ranked[0][0])

    if top_candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(
                    validate_plugin_file,
                    p,
                    file_cache,
                    cache_lock,
                    domain_cache,
                    domain_lock,
                    blacklist,
                )
                for p in top_candidates
            ]
            concurrent.futures.wait(futures)

    for key, variants in candidates.items():
        ranked = sorted(
            variants,
            key=lambda pair: plugin_variant_rank(pair[0], pair[1], seed_repos),
            reverse=True,
        )

        selected = None
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
            attempts.append({
                "repository": source_repo.get("repository"),
                "url": plugin.get("url"),
                "version": plugin.get("version"),
                "result": check.get("reason"),
            })
            if check.get("ok"):
                selected = (plugin, source_repo, check, variant_index)
                break
            rejection_counts[check.get("reason") or "unknown"] += 1

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
            "fallback_used": variant_index > 0,
            "attempts": attempts if variant_index > 0 else None,
        })

    published.sort(key=lambda x: (
        str(x.get("name") or x.get("internalName") or "").lower(),
        str(x.get("internalName") or "").lower(),
    ))
    health_rows.sort(key=lambda x: (
        x.get("status") != "healthy",
        str(x.get("name") or x.get("internal_name") or "").lower(),
    ))

    repo_manifest = {
        "name": "EmirTV",
        "description": "Otomatik doğrulanan ve tekilleştirilen Türkçe CloudStream eklenti deposu.",
        "iconUrl": "https://raw.githubusercontent.com/kodlama155-ctrl/csrepo/main/assets/emirtv-icon.png",
        "manifestVersion": 1,
        "pluginLists": [
            "https://raw.githubusercontent.com/kodlama155-ctrl/csrepo/main/plugins.json"
        ],
    }

    health = {
        "generated_at": now_iso(),
        "candidate_plugin_count": len(candidates),
        "healthy_plugin_count": len(published),
        "rejected_plugin_count": len(candidates) - len(published),
        "fallback_used_count": fallback_count,
        "rejection_attempt_counts": dict(sorted(rejection_counts.items())),
        "checks": {
            "status_zero_rejected": True,
            "http_200_required": True,
            "non_empty_required": True,
            "sha256_verified_when_provided": True,
            "dex_and_manifest_verified": True,
            "provider_domain_health_verified": True,
            "max_file_bytes": PLUGIN_MAX_BYTES,
            "executes_plugin_code": False,
        },
        "plugins": health_rows,
    }

    save_json(ROOT / "plugins.json", published)
    save_json(ROOT / "repo.json", repo_manifest)
    save_json(DATA / "plugin-health.json", health)
    return len(published), health["rejected_plugin_count"], fallback_count


def cloudstream_install_url(url: str) -> str:
    if url.startswith("https://"):
        return "cloudstreamrepo://" + url[len("https://"):]
    return url


def build_repo_catalog(app_ready: list[dict], published_plugin_count: int, seed_repos: set[str]):
    bundle_url = "https://raw.githubusercontent.com/kodlama155-ctrl/csrepo/main/repo.json"
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
    save_json(DATA / "catalog.json", catalog)

    # Simple official-style repository database: first our bundle, then original repos.
    repo_urls = [bundle_url]
    seen = {bundle_url}
    for item in repos:
        url = item["repo_url"]
        if url not in seen:
            seen.add(url)
            repo_urls.append(url)
    save_json(ROOT / "repos-db.json", repo_urls)

    lines = [
        "# EmirTV — Depoları Seç",
        "",
        "İsterseniz tüm Türkçe eklentileri tek depoda, isterseniz kaynak depoları ayrı ayrı ekleyebilirsiniz.",
        "",
        "## Hepsi Bir Arada",
        "",
        f"- [EmirTV — Hepsi Bir Arada]({bundle['install_url']}) — {published_plugin_count} tekilleştirilmiş eklenti",
        "",
        "## Tek Tek Depolar",
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
    (ROOT / "catalog.md").write_text("\n".join(lines), encoding="utf-8")
    build_main_readme(repos, bundle, generated_at)

    return len(repos)


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    old_tr = load_json(DATA / "repos.json", [])
    seeds = load_json(DATA / "seeds.json", [])
    seed_repos = {x["repository"] for x in seeds}
    seed_owners = {x.split("/", 1)[0] for x in seed_repos}

    candidates = discover_candidates()
    print(f"[info] candidates: {len(candidates)}")

    turkish = []
    global_other = []

    for i, (full_name, sources) in enumerate(sorted(candidates.items()), 1):
        try:
            item = inspect_repo(full_name, sources)
            if not item:
                print(f"[skip] {full_name}: no valid CloudStream repo.json")
                continue

            cls = turkish_classification(item, sources, seed_repos, seed_owners)
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
            print(f"[warn] {full_name}: {type(e).__name__}: {e}")

        if i % 20 == 0:
            time.sleep(1)

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

    published_plugin_count, rejected_plugin_count, plugin_fallback_count = build_cloudstream_bundle(app_ready, seed_repos)
    catalog_repo_count = build_repo_catalog(app_ready, published_plugin_count, seed_repos)

    changes = summarize_changes(old_tr, app_ready)
    save_json(DATA / "candidates.json", sorted(candidates))
    save_json(DATA / "repos.json", app_ready)
    save_json(DATA / "ranking.json", [
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
    save_json(DATA / "turkish_all.json", turkish)
    save_json(DATA / "global.json", global_other)
    save_json(DATA / "changes.json", {
        "generated_at": now_iso(),
        "candidate_count": len(candidates),
        "verified_count": len(turkish) + len(global_other),
        "turkish_count": len(turkish),
        "app_ready_count": len(app_ready),
        "published_plugin_count": published_plugin_count,
        "rejected_plugin_count": rejected_plugin_count,
        "plugin_fallback_count": plugin_fallback_count,
        "catalog_repo_count": catalog_repo_count,
        "global_count": len(global_other),
        "changes": changes,
    })

    print(
        f"[done] verified={len(turkish) + len(global_other)} "
        f"turkish={len(turkish)} app_ready={len(app_ready)} "
        f"published_plugins={published_plugin_count} "
        f"rejected_plugins={rejected_plugin_count} "
        f"fallbacks={plugin_fallback_count} "
        f"catalog_repos={catalog_repo_count} "
        f"global={len(global_other)} changes={len(changes)}"
    )


if __name__ == "__main__":
    main()
