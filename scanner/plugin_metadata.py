"""Metadata helpers shared by the scanner and the phone bridge."""

import re


def normalize_sha256(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.lower().startswith(("sha256-", "sha256:")):
        cleaned = cleaned[7:]
    if re.fullmatch(r"[a-fA-F0-9]{64}", cleaned):
        return cleaned.lower()
    return None


def select_live_plugins(plugins: list[dict]) -> list[dict]:
    return [
        plugin for plugin in plugins
        if isinstance(plugin.get("tvTypes"), list) and "Live" in plugin["tvTypes"]
    ]
