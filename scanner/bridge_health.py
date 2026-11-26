#!/usr/bin/env python3
"""Köprü (telefon) taramasının sonuçlarını eklenti listesine işler.

`data/health.json` dosyası EmirTV Köprü uygulaması tarafından yazılır:
her eklenti gerçekten çalıştırılır, arama/detay/link zinciri denenir.

Bu betik dosyası eşleşen eklentilere `bridge` alanı ekler.
Sonuçlar `data/bridge-results.json` dosyasında eklenti başına tek kayıt
olarak saklanır (7 gün); böylece her tarama listeyi sıfırdan yazsa da
köprü sonuçları kaybolmaz. Üst üste 3 kez ölü çıkan eklenti listeden
çıkarılır, `unknown` sayılmaz.

Ana liste `plugins-x92.json`, canlı TV listesi `data/live-plugins.json`
ve tarama özeti `data/bridge-health.json` dosyasına yazılır.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from os import linesep
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    from plugin_metadata import normalize_sha256, select_live_plugins
    from stages import publish_files
else:
    from scanner.plugin_metadata import normalize_sha256, select_live_plugins
    from scanner.stages import publish_files

ROOT = Path(__file__).resolve().parents[1]
HEALTH_PATH = ROOT / "data" / "health.json"
PLUGINS_PATH = ROOT / "plugins-x92.json"
LIVE_PLUGINS_PATH = ROOT / "data" / "live-plugins.json"
SUMMARY_PATH = ROOT / "data" / "bridge-health.json"
RESULTS_PATH = ROOT / "data" / "bridge-results.json"

MAX_AGE_DAYS = 14
RESULTS_MAX_AGE_DAYS = 7
REMOVE_AFTER_DEAD_STREAK = 3


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def json_bytes(value) -> bytes:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return text.replace("\n", linesep).encode("utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def plugin_key(row) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("internalName") or row.get("internal_name") or "").strip().lower()


def parse_stamp(stamp):
    if not stamp:
        return None
    text = str(stamp).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_fresh(stamp: str, days: int = MAX_AGE_DAYS) -> bool:
    parsed = parse_stamp(stamp)
    if parsed is None:
        return False
    return datetime.now(timezone.utc) - parsed <= timedelta(days=days)


def matches_plugin_file(plugin, row) -> bool:
    plugin_hash = plugin.get("fileHash")
    tested_hash = row.get("hash")
    if plugin_hash and tested_hash:
        digest = normalize_sha256(plugin_hash)
        return digest is not None and digest == normalize_sha256(tested_hash)

    version = plugin.get("version")
    tested_version = row.get("version")
    url = plugin.get("url")
    return (
        version not in (None, "")
        and tested_version is not None
        and str(version) == str(tested_version)
        and isinstance(url, str)
        and bool(url)
        and url == row.get("url")
    )


def record_matches_plugin(record, plugin) -> bool:
    record_hash = normalize_sha256(record.get("fileHash"))
    plugin_hash = normalize_sha256(plugin.get("fileHash"))
    if record_hash and plugin_hash:
        return record_hash == plugin_hash

    version = record.get("version")
    return (
        version not in (None, "")
        and str(version) == str(plugin.get("version"))
        and isinstance(plugin.get("url"), str)
        and bool(plugin.get("url"))
        and plugin.get("url") == record.get("url")
    )


def load_results(path: Path) -> dict[str, dict]:
    payload = load_json(path, None)
    rows = payload.get("plugins") if isinstance(payload, dict) else payload
    results = {}
    for row in rows or []:
        key = plugin_key(row)
        if key and isinstance(row, dict):
            results[key] = row
    return results


def prune_results(results: dict[str, dict]) -> dict[str, dict]:
    return {
        key: record for key, record in results.items()
        if is_fresh(record.get("checked_at"), RESULTS_MAX_AGE_DAYS)
    }


def update_results(results, health, plugins):
    """Taze köprü raporunu kalıcı kayıtlara işler; unknown sayılmaz."""
    index = {plugin_key(plugin): plugin for plugin in plugins}
    stamp = health.get("generated_at")
    for row in health.get("plugins") or []:
        key = plugin_key(row)
        plugin = index.get(key)
        if not key or plugin is None or not matches_plugin_file(plugin, row):
            continue

        status = str(row.get("status") or "").strip().lower()
        if status not in ("ok", "alive", "dead"):
            continue

        previous = results.get(key)
        same_file = previous is not None and record_matches_plugin(previous, plugin)
        streak = int(previous.get("dead_streak") or 0) if same_file else 0
        if status == "dead":
            streak += 1
        else:
            streak = 0

        results[key] = {
            "internal_name": key,
            "name": plugin.get("name") or row.get("name"),
            "url": plugin.get("url"),
            "version": plugin.get("version"),
            "fileHash": plugin.get("fileHash"),
            "status": status,
            "links": row.get("links"),
            "ms": row.get("ms"),
            "checked_at": stamp,
            "dead_streak": streak,
        }
    return results


def apply_results(results, plugins):
    """Kayıtlı sonuçları listeye işler; 3 kez üst üste ölüyü çıkarır."""
    matched = 0
    removed = []
    remaining = []
    for plugin in plugins:
        key = plugin_key(plugin)
        record = results.get(key)
        if record is None or not record_matches_plugin(record, plugin):
            remaining.append(plugin)
            continue

        matched += 1
        bridge = {
            "status": record.get("status"),
            "links": record.get("links"),
            "ms": record.get("ms"),
            "checked_at": record.get("checked_at"),
            "dead_streak": int(record.get("dead_streak") or 0),
        }
        if bridge["status"] == "dead" and bridge["dead_streak"] >= REMOVE_AFTER_DEAD_STREAK:
            removed.append({
                "internal_name": key,
                "name": plugin.get("name"),
                "dead_streak": bridge["dead_streak"],
                "checked_at": bridge["checked_at"],
            })
            continue
        plugin["bridge"] = bridge
        remaining.append(plugin)
    return matched, removed, remaining


def main() -> int:
    plugins = load_json(PLUGINS_PATH, [])
    if not isinstance(plugins, list) or not plugins:
        print("bridge health: eklenti listesi okunamadi")
        return 1

    results = prune_results(load_results(RESULTS_PATH))

    health = load_json(HEALTH_PATH, None)
    if not isinstance(health, dict):
        health = {}
    if not (health.get("plugins") or []):
        print("bridge health: veri yok, kayitli sonuclar kullanildi")
    elif not is_fresh(health.get("generated_at")):
        print("bridge health: veri eski, kayitli sonuclar kullanildi")
    else:
        results = update_results(results, health, plugins)

    matched, removed, remaining = apply_results(results, plugins)

    summary = {
        "generated_at": health.get("generated_at"),
        "source": health.get("source"),
        "total": health.get("total"),
        "done": health.get("done"),
        "ok": health.get("ok"),
        "empty": health.get("empty"),
        "dead": health.get("dead"),
        "matched": matched,
        "stored": len(results),
        "removed": removed,
        "removed_count": len(removed),
        "plugins": health.get("plugins") or [],
    }
    if not health.get("plugins"):
        # Telefon sessizken son rapor bilgisi özet dosyasında korunur.
        previous = load_json(SUMMARY_PATH, {})
        if isinstance(previous, dict):
            for field in ("generated_at", "source", "total", "done", "ok", "empty", "dead"):
                summary[field] = previous.get(field)
    with publish_files({
        PLUGINS_PATH: json_bytes(remaining),
        LIVE_PLUGINS_PATH: json_bytes(select_live_plugins(remaining)),
        SUMMARY_PATH: json_bytes(summary),
        RESULTS_PATH: json_bytes({
            "generated_at": now_iso(),
            "plugins": [results[key] for key in sorted(results)],
        }),
    }):
        print(
            f"bridge health: {matched} eslesti, {len(removed)} cikarildi "
            f"(ok={summary['ok']}, empty={summary['empty']}, dead={summary['dead']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
