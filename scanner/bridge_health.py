#!/usr/bin/env python3
"""Köprü (telefon) taramasının sonuçlarını eklenti listesine işler.

`data/health.json` dosyası EmirTV Köprü uygulaması tarafından yazılır:
her eklenti gerçekten çalıştırılır, arama/detay/link zinciri denenir.

Bu betik dosyası eşleşen eklentilere `bridge` alanı ekler.
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

MAX_AGE_DAYS = 14


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def json_bytes(value) -> bytes:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return text.replace("\n", linesep).encode("utf-8")


def plugin_key(row) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("internalName") or row.get("internal_name") or "").strip().lower()


def is_fresh(stamp: str) -> bool:
    if not stamp:
        return False
    text = str(stamp).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed <= timedelta(days=MAX_AGE_DAYS)


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


def merge(health, plugins):
    """health kayıtlarını plugin listesine işler; eşleşen sayısını döner."""
    index = {}
    for row in health.get("plugins") or []:
        key = plugin_key(row)
        if key:
            index[key] = row

    checked_at = health.get("generated_at")
    matched = 0
    for plugin in plugins:
        row = index.get(plugin_key(plugin))
        if row is None or not matches_plugin_file(plugin, row):
            continue
        plugin["bridge"] = {
            "status": row.get("status"),
            "links": row.get("links"),
            "ms": row.get("ms"),
            "checked_at": checked_at,
        }
        matched += 1
    return matched


def main() -> int:
    health = load_json(HEALTH_PATH, None)
    if not isinstance(health, dict) or not (health.get("plugins") or []):
        print("bridge health: veri yok, atlandi")
        return 0
    if not is_fresh(health.get("generated_at")):
        print("bridge health: veri eski, atlandi")
        return 0

    plugins = load_json(PLUGINS_PATH, [])
    if not isinstance(plugins, list) or not plugins:
        print("bridge health: eklenti listesi okunamadi")
        return 1

    matched = merge(health, plugins)
    summary = {
        "generated_at": health.get("generated_at"),
        "source": health.get("source"),
        "total": health.get("total"),
        "done": health.get("done"),
        "ok": health.get("ok"),
        "empty": health.get("empty"),
        "dead": health.get("dead"),
        "matched": matched,
        "plugins": health.get("plugins") or [],
    }
    with publish_files({
        PLUGINS_PATH: json_bytes(plugins),
        LIVE_PLUGINS_PATH: json_bytes(select_live_plugins(plugins)),
        SUMMARY_PATH: json_bytes(summary),
    }):
        print(
            f"bridge health: {matched} eslesti "
            f"(ok={health.get('ok')}, empty={health.get('empty')}, dead={health.get('dead')})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
