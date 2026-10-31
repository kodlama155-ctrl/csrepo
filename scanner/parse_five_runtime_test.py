#!/usr/bin/env python3
"""Parse CloudStream instrumentation logcat; never equate link tests to playback."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERN = re.compile(r"RESULT\|([^|]+)\|([^|]+)\|(\d+)\|(\d+)\|([^\s|]+)")


def main() -> int:
    raw = (ROOT / "five-provider-logcat.txt")
    selection = (ROOT / "five-plugin-selection.json")
    output = ROOT / "five-provider-runtime-results.json"
    selected = json.loads(selection.read_text(encoding="utf-8"))
    text = raw.read_text(encoding="utf-8", errors="replace") if raw.exists() else ""
    results = {}
    for match in PATTERN.finditer(text):
        name, status, millis, provider_count, reason = match.groups()
        results[name] = {
            "status": status,
            "reason": reason,
            "duration_seconds": round(int(millis) / 1000, 3),
            "registered_providers": int(provider_count),
            "video_playback_verified": False,
        }
    records = []
    for p in selected:
        internal = p["internalName"]
        records.append({
            **p,
            **results.get(internal, {
                "status": "not_tested",
                "reason": "no_instrumentation_result",
                "duration_seconds": None,
                "registered_providers": 0,
                "video_playback_verified": False,
            })
        })
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": "cloudstream_real_android_provider_runtime",
        "warning": "A runtime_pass proves CloudStream provider search/load/loadLinks checks only; no ExoPlayer playback was performed.",
        "requested": len(records),
        "reported": sum(x["status"] != "not_tested" for x in records),
        "runtime_passed": sum(x["status"] == "runtime_pass" for x in records),
        "results": records,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for entry in records:
        print(f"{entry['internalName']}: {entry['status']} ({entry['reason']}) seconds={entry['duration_seconds']}")
    print(f"CloudStream runtime provider checks: {report['reported']}/{report['requested']} reported, {report['runtime_passed']} passed.")
    print("Actual decoded video playback has NOT been verified.")
    return 0 if report["reported"] == report["requested"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
