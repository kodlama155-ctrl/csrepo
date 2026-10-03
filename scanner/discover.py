#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

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


def discover_candidates() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def add(full_name: str, source: str):
        if "/" in full_name:
            found.setdefault(full_name, set()).add(source)

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

    # Expand owners of trusted Turkish seeds so sibling projects are not missed.
    seed_owners = sorted({x["repository"].split("/", 1)[0] for x in seeds})
    for owner in seed_owners:
        try:
            page = 1
            while page <= 3:
                repos = api_get(f"/users/{quote(owner)}/repos", {
                    "per_page": 100, "page": page, "sort": "pushed", "direction": "desc"
                })
                if not repos:
                    break
                for item in repos:
                    name = item.get("name", "")
                    desc = item.get("description") or ""
                    haystack = f"{name} {desc}".lower()
                    if any(k in haystack for k in ("cloudstream", "wio", "kekik", "cs-plugin", "csrepo")):
                        add(item["full_name"], f"owner-expand:{owner}")
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
        }
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "url": url, "reachable": False, "count": 0, "updated_at": None,
            "languages": {}, "turkish_language_count": 0, "text_sample": "",
        }

    if isinstance(payload, list):
        plugins = payload
    elif isinstance(payload, dict) and isinstance(payload.get("plugins"), list):
        plugins = payload["plugins"]
    else:
        plugins = []

    languages = Counter()
    text_bits = []
    for p in plugins:
        if not isinstance(p, dict):
            continue
        lang = normalize_language(p.get("language"))
        if lang:
            languages[lang] += 1
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
            })
            continue
        try:
            plugin_infos.append(plugin_list_info(url))
        except requests.RequestException:
            plugin_infos.append({
                "url": url, "reachable": False, "count": 0, "updated_at": None,
                "languages": {}, "turkish_language_count": 0, "text_sample": "",
            })

    plugin_count = sum(x.get("count", 0) for x in plugin_infos)
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
        "repository": full_name,
        "repository_url": meta.get("html_url"),
        "repo_url": repo_json_url,
        "manifest_branch": branch,
        "manifest_version": manifest.get("manifestVersion", 1),
        "plugin_lists": plugin_infos,
        "plugin_count": plugin_count,
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
                # Normal rule: only Turkish plugins. If the plugin has no language
                # metadata, keep it only when its source repo is explicitly Turkish
                # or trusted.
                if lang not in TR_LANGUAGE_VALUES:
                    if lang is not None or not (explicit_repo or trusted_repo):
                        continue

                key = (
                    str(plugin.get("internalName") or "").strip()
                    or str(plugin.get("name") or "").strip()
                    or str(plugin.get("url") or "").strip()
                )
                if not key:
                    continue
                candidates.setdefault(key, []).append((plugin, repo_item))

    merged = []
    for key, variants in candidates.items():
        def rank(pair):
            plugin, repo_item = pair
            version = plugin.get("version")
            try:
                version_num = int(version)
            except (TypeError, ValueError):
                version_num = -1
            trusted = 1 if repo_item.get("repository") in seed_repos else 0
            fresh = parse_iso(repo_item.get("last_plugins_update")).timestamp()
            tr_score = int(repo_item.get("turkish", {}).get("score") or 0)
            return (version_num, fresh, trusted, tr_score)

        plugin, source_repo = max(variants, key=rank)
        out = dict(plugin)
        out["_csrepoSource"] = source_repo.get("repository")
        merged.append(out)

    merged.sort(key=lambda x: (
        str(x.get("name") or x.get("internalName") or "").lower(),
        str(x.get("internalName") or "").lower(),
    ))

    # Remove our audit-only field before publishing to CloudStream.
    published = []
    for plugin in merged:
        clean = dict(plugin)
        clean.pop("_csrepoSource", None)
        published.append(clean)

    repo_manifest = {
        "name": "Emir CloudStream",
        "description": "Otomatik doğrulanan ve tekilleştirilen Türkçe CloudStream eklenti deposu.",
        "manifestVersion": 1,
        "pluginLists": [
            "https://raw.githubusercontent.com/kodlama155-ctrl/csrepo/main/plugins.json"
        ],
    }

    save_json(ROOT / "plugins.json", published)
    save_json(ROOT / "repo.json", repo_manifest)
    return len(published)


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

    published_plugin_count = build_cloudstream_bundle(app_ready, seed_repos)

    changes = summarize_changes(old_tr, app_ready)
    save_json(DATA / "candidates.json", sorted(candidates))
    save_json(DATA / "repos.json", app_ready)
    save_json(DATA / "turkish_all.json", turkish)
    save_json(DATA / "global.json", global_other)
    save_json(DATA / "changes.json", {
        "generated_at": now_iso(),
        "candidate_count": len(candidates),
        "verified_count": len(turkish) + len(global_other),
        "turkish_count": len(turkish),
        "app_ready_count": len(app_ready),
        "published_plugin_count": published_plugin_count,
        "global_count": len(global_other),
        "changes": changes,
    })

    print(
        f"[done] verified={len(turkish) + len(global_other)} "
        f"turkish={len(turkish)} app_ready={len(app_ready)} "
        f"published_plugins={published_plugin_count} "
        f"global={len(global_other)} changes={len(changes)}"
    )


if __name__ == "__main__":
    main()
