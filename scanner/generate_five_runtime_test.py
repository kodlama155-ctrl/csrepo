#!/usr/bin/env python3
"""Generate a five-plugin Android instrumentation test against real CloudStream.

Source: current CSRepo plugins-x92.json. Picks one plugin from each distinct
upstream repository, with a stable deterministic order.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scanner" / "CsrepoFivePluginTest.kt.txt"
TARGET_NAME = "CsrepoFivePluginTest.kt"


def kotlin_string(value):
    return json.dumps(str(value), ensure_ascii=True)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: generate_five_runtime_test.py CLOUDSTREAM_CHECKOUT", file=sys.stderr)
        return 2
    checkout = Path(sys.argv[1]).resolve()
    target = checkout / "app/src/androidTest/java/com/lagradost/cloudstream3" / TARGET_NAME
    selected = []
    repositories = set()
    for p in json.loads((ROOT / "plugins-x92.json").read_text(encoding="utf-8")):
        repo = p.get("repositoryUrl")
        if not repo or repo in repositories or not p.get("url") or not p.get("internalName"):
            continue
        repositories.add(repo)
        selected.append(p)
        if len(selected) == 5:
            break
    if len(selected) != 5:
        print(f"Not enough plugins from distinct repositories: {len(selected)}", file=sys.stderr)
        return 2

    cases = []
    for p in selected:
        fields = [
            kotlin_string(p.get("name") or p["internalName"]),
            kotlin_string(p["internalName"]),
            kotlin_string(p["url"]),
            kotlin_string(p["fileHash"]) if p.get("fileHash") else "null",
            kotlin_string(p["repositoryUrl"]),
        ]
        cases.append("        Case(" + ", ".join(fields) + ")")
    source = TEMPLATE.read_text(encoding="utf-8")
    marker = "        // CSREPO_SELECTED_PLUGIN_CASES"
    assert marker in source
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.replace(marker, ",\n".join(cases)), encoding="utf-8")
    (ROOT / "five-plugin-selection.json").write_text(
        json.dumps(
            [
                {
                    "name": p.get("name"),
                    "internalName": p["internalName"],
                    "repositoryUrl": p["repositoryUrl"],
                }
                for p in selected
            ],
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"Generated Android provider test at {target}")
    print("Selected plugins: " + ", ".join(p["internalName"] for p in selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
