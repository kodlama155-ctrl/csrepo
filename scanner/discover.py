#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
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

CODE_QUERIES = [
    'filename:repo.json "pluginLists" "CloudStream"',
    'filename:repo.json "pluginLists" "Türkçe"',
]

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

    for query in SEARCH_QUERIES:
        try:
            result = api_get("/search/repositories", {"q": query, "per_page": 100, "page": 1})
            for item in result.get("items", []):
                add(item["full_name"], f"repo-search:{query}")
        except requests.RequestException as e:
            print(f"[warn] repository search failed: {query}: {e}")

    # Code search catches repositories whose name/description does not say CloudStream.
    for query in CODE_QUERIES:
        try:
            result = api_get("/search/code", {"q": query, "per_page": 100, "page": 1})
            for item in result.get("items", []):
                add(item["repository"]["full_name"], f"code-search:{query}")
            time.sleep(1)
        except requests.RequestException as e:
            print(f"[warn] code search failed: {query}: {e}")

    # Expand owners of trusted seed repos. This is what prevents sibling projects
    # such as WioSinema/WioAnime/WioDrama from being missed.
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


def plugin_list_info(url: str):
    raw, _ = text_get(url)
    if raw is None:
        return {"url": url, "reachable": False, "count": 0, "updated_at": None}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"url": url, "reachable": False, "count": 0, "updated_at": None}

    if isinstance(payload, list):
        count = len(payload)
    elif isinstance(payload, dict) and isinstance(payload.get("plugins"), list):
        count = len(payload["plugins"])
    else:
        count = 0

    updated_at = None
    parsed = parse_raw_github(url)
    if parsed:
        repo_full, ref, path = parsed
        updated_at = latest_commit_for_path(repo_full, ref, path)

    return {
        "url": url,
        "reachable": True,
        "count": count,
        "updated_at": updated_at,
        "source_repository": parsed[0] if parsed else None,
    }


def inspect_repo(full_name: str, discovery_sources: set[str]):
    owner, repo = full_name.split("/", 1)
    meta = api_get(f"/repos/{owner}/{repo}", allow_404=True)
    if not meta:
        return None
    if meta.get("private"):
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
            plugin_infos.append({"url": str(url), "reachable": False, "count": 0, "updated_at": None})
            continue
        try:
            plugin_infos.append(plugin_list_info(url))
        except requests.RequestException:
            plugin_infos.append({"url": url, "reachable": False, "count": 0, "updated_at": None})

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


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    old = load_json(DATA / "repos.json", [])
    candidates = discover_candidates()
    print(f"[info] candidates: {len(candidates)}")

    results = []
    for i, (full_name, sources) in enumerate(sorted(candidates.items()), 1):
        try:
            item = inspect_repo(full_name, sources)
            if item:
                results.append(item)
                print(f"[ok] {full_name}: {item['status']} / {item['plugin_count']} plugins")
            else:
                print(f"[skip] {full_name}: no valid CloudStream repo.json")
        except Exception as e:
            print(f"[warn] {full_name}: {type(e).__name__}: {e}")
        if i % 20 == 0:
            time.sleep(1)

    results.sort(key=lambda x: (
        x.get("status") != "active",
        x.get("name") or "",
        x.get("repository") or "",
    ))

    changes = summarize_changes(old, results)
    save_json(DATA / "repos.json", results)
    save_json(DATA / "changes.json", {
        "generated_at": now_iso(),
        "candidate_count": len(candidates),
        "verified_count": len(results),
        "changes": changes,
    })

    print(f"[done] verified={len(results)} changes={len(changes)}")


if __name__ == "__main__":
    main()
