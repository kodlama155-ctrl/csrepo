#!/usr/bin/env python3
"""Köprü (telefon) taramasının sonuçlarını eklenti listesine işler.

`data/health.json` dosyası EmirTV Köprü uygulaması tarafından yazılır:
her eklenti gerçekten çalıştırılır, arama/detay/link zinciri denenir.

Bu betik sonucu `plugins-x92.json` içindeki kayıtlara `bridge` alanı olarak ekler
ve özeti `data/bridge-health.json` dosyasına yazar. Eklenti listesinden kayıt
silmez; yalnızca durum bilgisi ekler.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEALTH_PATH = ROOT / "data" / "health.json"
PLUGINS_PATH = ROOT / "plugins-x92.json"
SUMMARY_PATH = ROOT / "data" / "bridge-health.json"

MAX_AGE_DAYS = 14


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        if row is None:
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
    save_json(PLUGINS_PATH, plugins)
    save_json(SUMMARY_PATH, {
        "generated_at": health.get("generated_at"),
        "source": health.get("source"),
        "total": health.get("total"),
        "done": health.get("done"),
        "ok": health.get("ok"),
        "empty": health.get("empty"),
        "dead": health.get("dead"),
        "matched": matched,
        "plugins": health.get("plugins") or [],
    })
    print(
        f"bridge health: {matched} eslesti "
        f"(ok={health.get('ok')}, empty={health.get('empty')}, dead={health.get('dead')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
