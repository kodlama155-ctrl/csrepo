"""Select validated plugins and write the combined CloudStream bundle."""

from __future__ import annotations

import concurrent.futures
import re
import threading
from collections import Counter
from pathlib import Path

from . import plugin_validation, repository_discovery, scan_utils
from .plugin_metadata import normalize_sha256, select_live_plugins
from .repository_discovery import TR_LANGUAGE_VALUES, normalize_language
from .scan_utils import now_iso, parse_iso, save_json


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


# Provider check results that reflect a scanner limitation rather than a dead site.
# Plugins with only these results stay published, marked provider_check="unverified".
CHECKER_LIMIT_REASONS = frozenset({"dynamic-domain-missing", "no-urls"})


def candidate_key(plugin: dict) -> str:
    raw = (
        str(plugin.get("internalName") or "").strip()
        or str(plugin.get("name") or "").strip()
        or str(plugin.get("url") or "").strip()
    )
    if not raw:
        return ""
    norm = raw.lower()
    m = re.match(r"^0\s+([a-zA-Z].*)", norm)
    return m.group(1).strip() if m else norm


def _is_transient_reason_part(part: str) -> bool:
    code = part.rsplit(":", 1)[-1].strip().lower()
    if code in ("unreachable", "unreachable-error", "dns-error", "http-408", "http-425", "http-429"):
        return True
    if code.startswith("http-") and code[5:].isdigit():
        return int(code[5:]) >= 500
    return False


def is_transient_provider_failure(reason) -> bool:
    """Site check failure that may be temporary (timeout, DNS, HTTP 5xx)."""
    if not reason:
        return False
    parts = [part.strip() for part in str(reason).split(";") if part.strip()]
    return bool(parts) and all(_is_transient_reason_part(part) for part in parts)


def previous_published(output_root: Path | None = None) -> dict[str, dict]:
    """Last published list, used to keep verified records through a temporary outage."""
    root = scan_utils.ROOT if output_root is None else output_root
    rows = scan_utils.load_json(root / "plugins-x92.json", [])
    index = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                key = candidate_key(row)
                if key:
                    index[key] = row
    return index


def keep_previous_verified(previous_row, transient) -> dict | None:
    """Keep the previous record for one scan if only the site check failed and the file is unchanged."""
    if not isinstance(previous_row, dict) or previous_row.get("recheck"):
        return None
    if transient is None:
        return None
    previous_hash = normalize_sha256(previous_row.get("fileHash"))
    current_hash = normalize_sha256(transient[2].get("actual_hash"))
    if not previous_hash or not current_hash or previous_hash != current_hash:
        return None
    kept = dict(previous_row)
    kept["recheck"] = True
    return kept


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
            entries = repository_discovery.fetch_plugin_entries(url)
            if not entries and (plist.get("count") or 0) > 0:
                raise RuntimeError("Plugin list changed or could not be read; previous results retained.")

            for plugin in entries:
                lang = normalize_language(plugin.get("language"))
                if lang not in TR_LANGUAGE_VALUES:
                    if lang is not None or not (explicit_repo or trusted_repo):
                        continue

                key = candidate_key(plugin)
                if not key:
                    continue

                candidates.setdefault(key, []).append((plugin, repo_item))

    published = []
    health_rows = []
    file_cache: dict[str, dict] = {}
    cache_lock = threading.Lock()
    domain_cache: dict[str, tuple[bool, str]] = {}
    domain_lock = threading.Lock()
    blacklist = plugin_validation.load_blacklist()
    rejection_counts = Counter()
    fallback_count = 0
    recheck_count = 0
    previous = previous_published(output_root)

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
                    plugin_validation.validate_plugin_file,
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
        transient = None
        attempts = []
        for variant_index, (plugin, source_repo) in enumerate(ranked):
            check = plugin_validation.validate_plugin_file(
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
            if (transient is None and check.get("ok")
                    and check.get("provider_check") == "unverified"
                    and is_transient_provider_failure(check.get("provider_reason"))):
                transient = (plugin, source_repo, check, variant_index)
            rejection_counts[reason or "unknown"] += 1

        if not selected and unverifiable:
            # File checks passed; only the scanner could not find a site address to test.
            selected = unverifiable

        if not selected:
            kept = keep_previous_verified(previous.get(key), transient)
            if kept is not None:
                published.append(kept)
                recheck_count += 1
                health_rows.append({
                    "internal_name": key,
                    "name": kept.get("name"),
                    "status": "recheck",
                    "reason": transient[2].get("provider_reason") or "provider-unverified",
                    "url": kept.get("url"),
                    "version": kept.get("version"),
                    "checked_at": now_iso(),
                    "attempts": attempts,
                })
                continue
            health_rows.append({
                "internal_name": key,
                "status": "rejected",
                "reason": reason or "unknown",
                "checked_at": now_iso(),
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
        "recheck_plugin_count": recheck_count,
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
            "transient_site_failure_grace": "one-scan",
            "max_file_bytes": plugin_validation.PLUGIN_MAX_BYTES,
            "executes_plugin_code": False,
        },
        "plugins": health_rows,
    }

    target_root = scan_utils.ROOT if output_root is None else output_root
    target_data = scan_utils.DATA if output_root is None else output_root / "data"
    save_json(target_root / PRIVATE_PLUGINS, published)
    save_json(target_data / "live-plugins.json", select_live_plugins(published))
    save_json(target_root / PRIVATE_MANIFEST, repo_manifest)
    save_json(target_root / "plugins.json", [])
    save_json(target_root / "repo.json", legacy_manifest)
    save_json(target_data / "plugin-health.json", health)
    return len(published), health["rejected_plugin_count"], fallback_count
