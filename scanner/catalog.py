"""Write the repository catalog, rankings, reports, and README."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import scan_utils
from .scan_utils import load_json, now_iso, parse_iso, save_json


IMPORTANT_REPOSITORIES = (
    "Wiojelt/TurkSinema",
    "Wiojelt/WioSinema",
    "feroxx/Kekik-cloudstream",
    "lepotane/MRC-builds",
    "Wiojelt/TurkSpor",
    "Emre-Kahveci/CloudStreamHub",
    "manitux-app/cs-plugins",
)

README_AUTO_START = "<!-- AUTO_REPOS_START -->"
README_AUTO_END = "<!-- AUTO_REPOS_END -->"


def markdown_cell(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def bridge_age_text(minutes: int) -> str:
    if minutes < 60:
        return f"{max(minutes, 1)} dakika önce"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} saat önce"
    return f"{hours // 24} gün önce"


def bridge_status_line() -> str | None:
    """Telefon köprüsünün son tarama özeti (README için)."""
    payload = scan_utils.load_json(scan_utils.DATA / "health.json", None)
    if not isinstance(payload, dict) or not payload.get("generated_at"):
        return None
    stamp = parse_iso(payload.get("generated_at"))
    if stamp == datetime.min.replace(tzinfo=timezone.utc):
        return None
    minutes = max(int((datetime.now(timezone.utc) - stamp).total_seconds() // 60), 0)
    counts = {key: int(payload.get(key) or 0) for key in ("ok", "alive", "dead", "unknown")}
    total = int(payload.get("total") or 0)
    done = int(payload.get("done") or 0)
    line = (
        f"**Köprü:** son tarama {bridge_age_text(minutes)} — "
        f"son turda ok {counts['ok']}, alive {counts['alive']}, dead {counts['dead']}, unknown {counts['unknown']} "
        f"({done}/{total} eklenti)"
    )
    if minutes >= 24 * 60:
        line += " — **UYARI: telefon sessiz**"
    return line


def build_main_readme(repos: list[dict], bundle: dict, generated_at: str, *, output_root: Path | None = None):
    by_repo = {item.get("repository"): item for item in repos}
    featured = [by_repo[x] for x in IMPORTANT_REPOSITORIES if x in by_repo]

    lines = [
        README_AUTO_START,
        "## 📦 Güncel Önemli Türkçe CloudStream Repoları",
        "",
        "Bu tablo bot tarafından otomatik güncellenir. Kısa kod/adresler kaynak repoların README dosyalarından tespit edilir.",
        "",
        f"**Son tarama:** <code>{generated_at}</code>",
        "",
    ]
    bridge = bridge_status_line()
    if bridge:
        lines.append(bridge)
        lines.append("")
    lines.extend([
        "| Repo | Durum | Eklenti | Kısa kod / adres | Uzun repo.json |",
        "|---|---|---:|---|---|",
    ])

    for item in featured:
        repo_name = markdown_cell(item.get("name") or item.get("repository"))
        repository_url = item.get("repository_url") or ""
        repo_label = f"[{repo_name}]({repository_url})" if repository_url else repo_name
        shorts = item.get("short_addresses") or []
        short_text = "<br>".join(f"<code>{markdown_cell(x)}</code>" for x in shorts) if shorts else "—"
        repo_url = markdown_cell(item.get("repo_url"))
        long_text = f"<code>{repo_url}</code>" if repo_url else "—"
        lines.append(
            f"| {repo_label} | {markdown_cell(item.get('status') or 'active')} | {int(item.get('plugin_count') or 0)} | {short_text} | {long_text} |"
        )

    lines.extend([
        "",
        "Tüm doğrulanmış depolar için [catalog.md](catalog.md) dosyasına bakın.",
        README_AUTO_END,
    ])
    auto_block = "\n".join(lines)

    readme_path = scan_utils.ROOT / "README.md"
    try:
        current = readme_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = "# csrepo\nCloudStream repo discovery and validation bot\n\n## Kısa Repo Adresleri\n\n- https://py.md/emirtv\n- !emirtv\n"

    if README_AUTO_START in current and README_AUTO_END in current:
        before = current.split(README_AUTO_START, 1)[0].rstrip()
        after = current.split(README_AUTO_END, 1)[1].lstrip()
        new_text = before + "\n\n" + auto_block
        if after:
            new_text += "\n\n" + after
    else:
        new_text = current.rstrip() + "\n\n" + auto_block

    target = (output_root / "README.md") if output_root is not None else readme_path
    target.write_text(new_text.rstrip() + "\n", encoding="utf-8")


def cloudstream_install_url(url: str) -> str:
    if url.startswith("https://"):
        return "cloudstreamrepo://" + url[len("https://"):]
    return url


def build_repo_catalog(app_ready: list[dict], published_plugin_count: int, seed_repos: set[str], *, output_root: Path | None = None):
    target_root = scan_utils.ROOT if output_root is None else output_root
    target_data = scan_utils.DATA if output_root is None else output_root / "data"
    bundle_url = "https://raw.githubusercontent.com/kodlama155-ctrl/csrepo/main/repo-x92.json"
    generated_at = now_iso()

    bundle = {
        "id": "emir-cloudstream-all",
        "type": "bundle",
        "name": "EmirTV — Hepsi Bir Arada",
        "description": "Aktif Türkçe CloudStream eklentilerinin otomatik doğrulanan ve tekilleştirilen birleşik deposu.",
        "repo_url": bundle_url,
        "install_url": cloudstream_install_url(bundle_url),
        "plugin_count": published_plugin_count,
        "source_repository_count": len(app_ready),
        "status": "active",
        "recommended": True,
    }

    repos = []
    for item in app_ready:
        repo_url = item.get("repo_url")
        if not isinstance(repo_url, str) or not repo_url.startswith(("https://", "http://")):
            continue
        repos.append({
            "id": item.get("repository"),
            "type": "repository",
            "name": item.get("name") or item.get("repository"),
            "description": item.get("description"),
            "icon_url": item.get("icon_url"),
            "repository": item.get("repository"),
            "repository_url": item.get("repository_url"),
            "repo_url": repo_url,
            "install_url": cloudstream_install_url(repo_url),
            "plugin_count": item.get("plugin_count") or 0,
            "repo_score": item.get("repo_score") or 0,
            "repo_score_details": item.get("repo_score_details") or {},
            "github_stars": item.get("github_stars") or 0,
            "github_forks": item.get("github_forks") or 0,
            "last_plugins_update": item.get("last_plugins_update"),
            "short_addresses": item.get("short_addresses") or [],
            "fork": bool(item.get("fork")),
            "parent": item.get("parent"),
            "self_contained": bool(item.get("self_contained")),
            "trusted_seed": item.get("repository") in seed_repos,
            "turkish_score": item.get("turkish", {}).get("score") or 0,
            "status": item.get("status"),
        })

    repos.sort(key=lambda x: (
        -(x.get("repo_score") or 0),
        not x.get("trusted_seed", False),
        -(x.get("plugin_count") or 0),
        str(x.get("name") or "").lower(),
    ))

    catalog = {
        "name": "EmirTV — Depoları Seç",
        "description": "Hepsi Bir Arada deposunu veya istediğiniz Türkçe CloudStream depolarını tek tek seçebilirsiniz.",
        "generated_at": generated_at,
        "sections": [
            {
                "id": "all",
                "title": "Hepsi Bir Arada",
                "items": [bundle],
            },
            {
                "id": "individual",
                "title": "Tek Tek Depolar",
                "items": repos,
            },
        ],
    }
    save_json(target_data / "catalog.json", catalog)

    # Simple official-style repository database: original repos.
    repo_urls = []
    seen = set()
    for item in repos:
        url = item["repo_url"]
        if url not in seen:
            seen.add(url)
            repo_urls.append(url)
    save_json(target_root / "repos-db.json", repo_urls)

    lines = [
        "# Türkçe CloudStream Depo Kataloğu",
        "",
        "Otomatik doğrulanan ve aktif Türkçe CloudStream eklenti depoları:",
        "",
        "## Doğrulanmış Kaynak Depolar",
        "",
    ]
    for item in repos:
        flags = []
        if item.get("trusted_seed"):
            flags.append("ana kaynak")
        if item.get("fork"):
            flags.append("fork")
        suffix = f" — RepoScore {item.get('repo_score', 0):.1f}/100 — {item['plugin_count']} eklenti"
        if flags:
            suffix += " — " + ", ".join(flags)
        lines.append(f"- [{item['name']}]({item['install_url']}){suffix}")
    lines.append("")
    (target_root / "catalog.md").write_text("\n".join(lines), encoding="utf-8")
    build_main_readme(repos, bundle, generated_at, output_root=output_root)

    return len(repos)


def summarize_changes(old: list[dict], new: list[dict]):
    old_by = {x.get("repository"): x for x in old}
    new_by = {x.get("repository"): x for x in new}
    changes = []

    for key in sorted(new_by.keys() - old_by.keys()):
        changes.append({"type": "new", "repository": key, "plugin_count": new_by[key].get("plugin_count")})
    for key in sorted(old_by.keys() - new_by.keys()):
        changes.append({"type": "missing", "repository": key})
    for key in sorted(new_by.keys() & old_by.keys()):
        a, b = old_by[key], new_by[key]
        if a.get("plugin_count") != b.get("plugin_count"):
            changes.append({
                "type": "plugin_count",
                "repository": key,
                "old": a.get("plugin_count"),
                "new": b.get("plugin_count"),
            })
        if a.get("status") != b.get("status"):
            changes.append({
                "type": "status",
                "repository": key,
                "old": a.get("status"),
                "new": b.get("status"),
            })
        if a.get("last_plugins_update") != b.get("last_plugins_update"):
            changes.append({
                "type": "plugins_updated",
                "repository": key,
                "old": a.get("last_plugins_update"),
                "new": b.get("last_plugins_update"),
            })
    return changes


def build_skip_report(candidates: dict[str, set[str]], inspection: dict, seed_repos: set[str]) -> dict[str, dict]:
    """One row per repository that did not enter the feed, with a fixed reason and its date.

    Every rejected candidate has to be accounted for here: stale, skipped, or sorted out as
    non-Turkish. Without the row we cannot tell why a repository disappeared.
    """
    rows = {}

    def put(row):
        name = row.get("repository")
        if not name:
            return
        rows[name] = {
            "repository": name,
            "skip_reason": row.get("skip_reason") or "unknown",
            "skip_detail": row.get("skip_detail"),
            "checked_at": row.get("checked_at") or now_iso(),
            "discovered_by": sorted(candidates.get(name) or []),
        }

    for row in inspection.get("stale_repositories") or []:
        put(row)
    for row in inspection.get("skipped") or []:
        put(row)

    feed = {x.get("repository") for x in inspection.get("app_ready") or []}
    others = {x.get("repository") for x in (inspection.get("turkish") or []) + (inspection.get("global_other") or [])}
    for name in others - feed:
        put({"repository": name, "skip_reason": "not-in-feed"})

    return dict(sorted(rows.items()))


def write_scan_outputs(candidates, inspection, old_tr, seeds, bundle_counts, *, output_root=None,
                       owner_probes=None, code_search=None):
    target_data = scan_utils.DATA if output_root is None else output_root / "data"
    turkish = inspection["turkish"]
    global_other = inspection["global_other"]
    stale_repositories = inspection["stale_repositories"]
    app_ready = inspection["app_ready"]
    seed_repos = {x["repository"] for x in seeds}
    published_plugin_count, rejected_plugin_count, plugin_fallback_count = bundle_counts

    catalog_repo_count = build_repo_catalog(app_ready, published_plugin_count, seed_repos, output_root=output_root)
    live_plugin_count = len(load_json(target_data / "live-plugins.json", []))

    changes = summarize_changes(old_tr, app_ready)
    skipped_repositories = build_skip_report(candidates, inspection, seed_repos)
    save_json(target_data / "candidates.json", sorted(candidates))
    save_json(target_data / "skipped.json", skipped_repositories)
    # Repositories checked and found without repo.json, keyed to their last push.
    save_json(target_data / "owner-probes.json", dict(sorted((owner_probes or {}).items())))
    save_json(target_data / "code-search.json", code_search or {})
    save_json(target_data / "repos.json", app_ready)
    save_json(target_data / "ranking.json", [
        {
            "rank": i,
            "repository": x.get("repository"),
            "name": x.get("name"),
            "repo_score": x.get("repo_score"),
            "repo_score_details": x.get("repo_score_details"),
            "plugin_count": x.get("plugin_count"),
            "status": x.get("status"),
        }
        for i, x in enumerate(app_ready, 1)
    ])
    save_json(target_data / "turkish_all.json", turkish)
    save_json(target_data / "global.json", global_other)
    save_json(target_data / "changes.json", {
        "generated_at": now_iso(),
        "candidate_count": len(candidates),
        "skipped_repo_count": len(skipped_repositories),
        "skipped_repositories": list(skipped_repositories.values()),
        "stale_repo_count": len(stale_repositories),
        "stale_repositories": stale_repositories,
        "verified_count": len(turkish) + len(global_other),
        "turkish_count": len(turkish),
        "app_ready_count": len(app_ready),
        "published_plugin_count": published_plugin_count,
        "live_plugin_count": live_plugin_count,
        "rejected_plugin_count": rejected_plugin_count,
        "plugin_fallback_count": plugin_fallback_count,
        "catalog_repo_count": catalog_repo_count,
        "global_count": len(global_other),
        "changes": changes,
    })

    return catalog_repo_count
