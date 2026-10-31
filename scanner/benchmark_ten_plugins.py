#!/usr/bin/env python3
"""Time ten independent CSRepo plugin preflight checks (NOT video playback).

This intentionally reuses the scanner's current validator, which checks the
download, file integrity, archive structure, and provider domain reachability.
It DOES NOT execute CloudStream Kotlin providers or decode/play a video.
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from discover import load_blacklist, validate_plugin_file

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "ten-plugin-preflight-benchmark.json"


def main() -> int:
    all_plugins = json.loads((ROOT / "plugins-x92.json").read_text(encoding="utf-8"))
    if not isinstance(all_plugins, list):
        print("plugins-x92.json is not an array", file=sys.stderr)
        return 2

    # Representative first ten distinct upstream repositories, deterministic.
    seen = set()
    selected = []
    for p in all_plugins:
        repo = p.get("repositoryUrl") or p.get("internalName") or p.get("name")
        if not repo or repo in seen or not p.get("url"):
            continue
        seen.add(repo)
        selected.append(p)
        if len(selected) == 10:
            break
    if len(selected) != 10:
        print(f"Expected 10 sources, found {len(selected)}", file=sys.stderr)
        return 2

    domain_cache: dict = {}
    domain_lock = threading.Lock()
    blacklist = load_blacklist()
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()

    def check(index_plugin):
        idx, plugin = index_plugin
        start = time.perf_counter()
        try:
            result = validate_plugin_file(
                plugin,
                {},
                domain_cache=domain_cache,
                domain_lock=domain_lock,
                blacklist=blacklist,
            )
            status = "preflight_pass" if result.get("ok") else "preflight_failed"
            reason = result.get("reason") or "unknown"
        except Exception as exc:
            status = "preflight_error"
            reason = type(exc).__name__
        return {
            "index": idx,
            "name": plugin.get("name"),
            "internal_name": plugin.get("internalName"),
            "repository": plugin.get("repositoryUrl"),
            "status": status,
            "reason": reason,
            "elapsed_seconds": round(time.perf_counter() - start, 3),
            "playback_verified": False,
        }

    print(f"Starting 10-plugin preflight benchmark with 4 workers; selected={len(selected)}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(check, enumerate(selected, 1)))
    elapsed = round(time.perf_counter() - t0, 3)
    report = {
        "generated_at": started_at,
        "kind": "ten_plugin_preflight_benchmark",
        "count": len(rows),
        "concurrency": 4,
        "elapsed_seconds": elapsed,
        "playback_verified": False,
        "important": "A pass verifies file/domain preflight only. It does NOT mean an episode plays.",
        "results": rows,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in rows:
        print(f"#{row['index']:02d} {row['name']}: {row['status']} ({row['reason']}) {row['elapsed_seconds']}s", flush=True)
    passed = sum(x["status"] == "preflight_pass" for x in rows)
    print(f"BENCHMARK completed: {passed}/{len(rows)} preflight pass; total {elapsed}s; playback NOT tested", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
