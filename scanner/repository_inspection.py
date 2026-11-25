"""Inspect, classify, and rank the repositories found during discovery."""

from __future__ import annotations

import concurrent.futures
import math
import threading
import time
from contextvars import copy_context
from datetime import datetime, timezone

import requests

from . import repository_discovery
from .scan_utils import now_iso, parse_iso


REPO_WORKERS = 4

# Strong Turkish ecosystem/content markers. Avoid generic words such as "film"
# by themselves because they are common in many languages.
TR_MARKERS = (
    "türk", "turk", "kekik", "eklenti", "sağlayıcı", "saglayici", "yayın",
    "yayin", "dizi", "izle", "belgesel", "çizgi", "cizgi", "inatbox",
    "rectv", "dizipal", "hdfilmcehennemi", "filmmakinesi", "turkanime",
    "sezonlukdizi", "sinewix", "sinema", "wiosinema", "wioanime",
    "wiodrama", "wioasya", "wiokids", "wiospor", "turkspor",
)


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
        meta = repository_discovery.api_get(f"/repos/{owner}/{repo}", allow_404=True)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 451:
            print(f"[skip] {full_name}: GitHub repository access blocked (HTTP 451)")
            return skip_repo(full_name, "access-blocked", "github-http-451")
        raise
    if not meta:
        return skip_repo(full_name, "repo-not-found", "github-http-404")
    if meta.get("private"):
        return skip_repo(full_name, "repo-private", "github-private")
    if repository_discovery.repo_is_stale(meta.get("pushed_at")):
        return {
            "repository": full_name,
            "status": "stale",
            "skip_reason": "repository-not-updated",
            "last_repo_push": meta["pushed_at"],
            "checked_at": now_iso(),
        }

    manifest_hit = None
    manifest_error = None
    for branch in repository_discovery.branch_names(full_name, meta.get("default_branch") or "main"):
        try:
            hit = repository_discovery.validate_manifest(full_name, branch)
        except requests.RequestException as error:
            manifest_error = error
            hit = None
        if hit:
            manifest_hit = (branch, *hit)
            break

    if not manifest_hit:
        if manifest_error is not None:
            raise manifest_error
        return skip_repo(full_name, "no-repo-json", "manifest-not-found")

    branch, repo_json_url, manifest = manifest_hit
    list_updates = {}
    for url in manifest.get("pluginLists", []):
        if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in list_updates:
            list_updates[url] = repository_discovery.plugin_list_update_info(url)
    if list_updates and all(repository_discovery.repo_is_stale(info[0]) for info in list_updates.values()):
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
            plugin_infos.append(repository_discovery.plugin_list_info(url, list_updates[url]))
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
    manifest_updated = repository_discovery.latest_commit_for_path(full_name, branch, "repo.json")
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


def skip_repo(full_name: str, reason: str, detail: str | None = None):
    """Every rejected candidate keeps one fixed reason and its date."""
    row = {
        "repository": full_name,
        "status": "skipped",
        "skip_reason": reason,
        "checked_at": now_iso(),
    }
    if detail:
        row["skip_detail"] = detail
    return row


def inspect_repositories(candidates: dict[str, set[str]]):
    """Inspect a few repositories concurrently; return results in stable order."""
    if not candidates:
        return
    sessions = []
    session_lock = threading.Lock()

    def initialize_worker():
        session = repository_discovery.open_worker_session()
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
                skipped.append({"repository": full_name, "skip_reason": "no-repo-json",
                                "checked_at": now_iso(),
                                "discovered_by": sorted(candidates[full_name])})
                continue
            if item.get("status") == "skipped":
                print(f"[skip] {full_name}: {item.get('skip_reason')}")
                skipped.append({**item, "discovered_by": sorted(candidates[full_name])})
                continue
            if item.get("status") == "stale":
                stale_repositories.append(item)
                print(f"[skip] {full_name}: {item.get('skip_reason', 'stale')} ({repository_discovery.MAX_REPO_IDLE_DAYS} days)")
                continue

            cls = turkish_classification(item, candidates[full_name], seed_repos, seed_owners)
            item["turkish"] = cls
            if cls["is_turkish"]:
                item["short_addresses"] = repository_discovery.readme_short_addresses(
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
