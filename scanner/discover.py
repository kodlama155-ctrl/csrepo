#!/usr/bin/env python3
"""Run the CloudStream scan stages from the command line or Python."""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner import repository_discovery, scan_utils, stages
from scanner.catalog import write_scan_outputs
from scanner.plugin_bundle import build_cloudstream_bundle
from scanner.repository_discovery import discover_candidates
from scanner.repository_inspection import inspect_candidates


def scan_api():
    return {
        "ROOT": scan_utils.ROOT,
        "DATA": scan_utils.DATA,
        "MAX_REPO_IDLE_DAYS": repository_discovery.MAX_REPO_IDLE_DAYS,
        "load_json": scan_utils.load_json,
        "now_iso": scan_utils.now_iso,
        "timed_phase": scan_utils.timed_phase,
        "shared_plugin_lists": repository_discovery.shared_plugin_lists,
        "discover_candidates": discover_candidates,
        "inspect_candidates": inspect_candidates,
        "build_cloudstream_bundle": build_cloudstream_bundle,
        "write_scan_outputs": write_scan_outputs,
    }


def main():
    return stages.run_stages(scan_api())


def cli(argv=None):
    return stages.run_cli(scan_api(), argv)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    cli()
