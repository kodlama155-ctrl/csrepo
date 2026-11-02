"""Persist scan stages independently and update public files only at stage four."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


LABELS = {
    1: "Depoları bulma",
    2: "Depoları inceleme",
    3: "Eklenti ve site kontrolleri",
    4: "Sonuçları hazırlama",
}
BUNDLE_FILES = (
    "plugins-x92.json", "repo-x92.json", "plugins.json", "repo.json", "data/plugin-health.json",
)
OUTPUT_FILES = BUNDLE_FILES + (
    "README.md", "repos-db.json", "catalog.md", "data/catalog.json", "data/ranking.json",
    "data/repos.json", "data/turkish_all.json", "data/global.json", "data/candidates.json", "data/changes.json",
    "data/owner-probes.json",
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def atomic_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def work_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    # OS locks are released even if the process stops unexpectedly.
    with (directory / ".lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Bu çalışma klasöründe başka bir tarama çalışıyor.") from error
        yield


@contextmanager
def publish_outputs(source, target):
    contents = {name: (source / name).read_bytes() for name in OUTPUT_FILES}
    before = {name: (target / name).read_bytes() if (target / name).exists() else None for name in OUTPUT_FILES}
    changed = []
    try:
        for name, content in contents.items():
            if content != before[name]:
                atomic_bytes(target / name, content)
                changed.append(name)
        yield
    except BaseException:
        for name in reversed(changed):
            if before[name] is None:
                (target / name).unlink(missing_ok=True)
            else:
                atomic_bytes(target / name, before[name])
        raise


class StageStore:
    def __init__(self, api, directory):
        self.api = api
        self.root = api["ROOT"].resolve()
        self.directory = Path(directory).resolve()

    def settings(self):
        return digest({"seeds": self.api["load_json"](self.api["DATA"] / "seeds.json", []),
                       "max_repo_idle_days": self.api["MAX_REPO_IDLE_DAYS"]})

    def blacklist_hash(self):
        return digest(self.api["load_json"](self.api["DATA"] / "blacklist.json", []))

    def artifact_path(self, relative):
        path = (self.directory / relative).resolve()
        if not path.is_relative_to(self.directory / "artifacts"):
            raise RuntimeError("Kaydedilmiş sonuç klasörü geçersiz.")
        return path

    def save(self, stage, payload, parent, elapsed, *, settings=None, blacklist_hash=None):
        if settings is not None and settings != self.settings():
            raise RuntimeError("Kaynaklar veya güncellik kuralı çalışma sırasında değişti; sonuç kaydedilmedi.")
        if blacklist_hash is not None and blacklist_hash != self.blacklist_hash():
            raise RuntimeError("Elenen eklenti listesi çalışma sırasında değişti; sonuç kaydedilmedi.")
        record = {
            "schema": 1, "stage": stage, "id": uuid.uuid4().hex, "root": str(self.root),
            "created_at": self.api["now_iso"](), "elapsed_seconds": round(elapsed, 3),
            "parent_id": parent["id"] if parent else None, "settings": self.settings(),
            "payload": payload, "payload_hash": digest(payload),
        }
        encoded = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_bytes(self.directory / f"stage-{stage}.json", encoded)
        return record

    def load(self, stage):
        path = self.directory / f"stage-{stage}.json"
        if not path.exists():
            raise RuntimeError(f"{stage}. aşamanın kaydı yok. Önce --stage {stage} çalıştırın.")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record["schema"] != 1 or record["stage"] != stage or record["root"] != str(self.root)
                    or not isinstance(record["id"], str) or not isinstance(record["settings"], str)
                    or not isinstance(record["payload"], dict) or not isinstance(record["elapsed_seconds"], (int, float))
                    or not isinstance(record["created_at"], str) or "parent_id" not in record
                    or record["payload_hash"] != digest(record["payload"])):
                raise ValueError("Invalid checkpoint")
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f"{stage}. aşamanın kaydı bozuk. --stage {stage} ile yenileyin.") from error
        if record["settings"] != self.settings():
            raise RuntimeError("Kaynaklar veya güncellik kuralı değişti. --stage 1 ile yeniden başlayın.")
        if stage > 1:
            parent = self.load(stage - 1)
            if record["parent_id"] != parent["id"]:
                raise RuntimeError(f"Önceki adım yenilenmiş. --stage {stage} ile bu adımı da yenileyin.")
        if stage == 3:
            payload = record["payload"]
            if payload.get("blacklist_hash") != self.blacklist_hash():
                raise RuntimeError("Elenen eklenti listesi değişti. --stage 3 ile kontrolleri yenileyin.")
            artifact = self.artifact_path(payload["artifact_dir"])
            for name in BUNDLE_FILES:
                file = artifact / name
                if not file.exists() or hashlib.sha256(file.read_bytes()).hexdigest() != payload["files"].get(name):
                    raise RuntimeError("Eklenti kontrolünün dosyaları eksik veya değişmiş. --stage 3 ile yenileyin.")
        return record

    def status(self):
        for stage, label in LABELS.items():
            try:
                record = self.load(stage)
            except RuntimeError as error:
                print(f"{stage}. {label}: hazır değil — {error}")
            else:
                print(f"{stage}. {label}: tamamlandı — {record['created_at']} — {record['elapsed_seconds']:.1f} sn")

    def execute(self, stage):
        parent = self.load(stage - 1) if stage > 1 else None
        first = self.load(1)["payload"] if stage > 1 else None
        settings = self.settings()
        blacklist_hash = self.blacklist_hash() if stage >= 3 else None
        started = time.perf_counter()
        with self.api["timed_phase"](LABELS[stage]):
            if stage == 1:
                old_repos = self.api["load_json"](self.api["DATA"] / "repos.json", [])
                seeds = self.api["load_json"](self.api["DATA"] / "seeds.json", [])
                probes = self.api["load_json"](self.api["DATA"] / "owner-probes.json", {})
                probes = probes if isinstance(probes, dict) else {}
                candidates = self.api["discover_candidates"](probe_memory=probes)
                if not candidates:
                    raise RuntimeError("Depo bulunamadı; önceki sonuçlar korundu.")
                payload = {
                    "candidates": {name: sorted(sources) for name, sources in sorted(candidates.items())},
                    "old_repos": old_repos, "seeds": seeds, "owner_probes": probes,
                }
                print(f"[info] candidates: {len(candidates)}")
            elif stage == 2:
                candidates = {name: set(sources) for name, sources in first["candidates"].items()}
                payload = self.api["inspect_candidates"](candidates, first["old_repos"], first["seeds"])
            elif stage == 3:
                artifact = self.directory / "artifacts" / uuid.uuid4().hex
                artifact.mkdir(parents=True)
                counts = self.api["build_cloudstream_bundle"](
                    parent["payload"]["app_ready"], {item["repository"] for item in first["seeds"]},
                    output_root=artifact,
                )
                payload = {
                    "bundle_counts": list(counts), "artifact_dir": str(artifact.relative_to(self.directory)),
                    "blacklist_hash": blacklist_hash,
                    "files": {name: hashlib.sha256((artifact / name).read_bytes()).hexdigest() for name in BUNDLE_FILES},
                }
            else:
                inspection = self.load(2)["payload"]
                source = self.artifact_path(parent["payload"]["artifact_dir"])
                # Keep stage three reusable: prepare stage four in its own directory.
                with tempfile.TemporaryDirectory(prefix="prepare-", dir=self.directory) as temporary:
                    prepared = Path(temporary).resolve()
                    if not prepared.is_relative_to(self.directory):
                        raise RuntimeError("Geçici çıktı klasörü geçersiz.")
                    for name in BUNDLE_FILES:
                        target = prepared / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes((source / name).read_bytes())
                    catalog_count = self.api["write_scan_outputs"](
                        first["candidates"], inspection, first["old_repos"], first["seeds"],
                        parent["payload"]["bundle_counts"], output_root=prepared,
                        owner_probes=first.get("owner_probes", {}),
                    )
                    payload = {"catalog_repo_count": catalog_count,
                               "output_hashes": {name: hashlib.sha256((prepared / name).read_bytes()).hexdigest()
                                                 for name in OUTPUT_FILES}}
                    summary = json.loads((prepared / "data/changes.json").read_text(encoding="utf-8"))
                    fields = {"verified": "verified_count", "turkish": "turkish_count", "app_ready": "app_ready_count",
                              "published_plugins": "published_plugin_count", "rejected_plugins": "rejected_plugin_count",
                              "fallbacks": "plugin_fallback_count", "catalog_repos": "catalog_repo_count",
                              "global": "global_count", "stale": "stale_repo_count"}
                    summary_text = "[done] " + " ".join(f"{key}={summary[value]}" for key, value in fields.items())
                    summary_text += f" changes={len(summary['changes'])}"
                    with publish_outputs(prepared, self.root):
                        record = self.save(stage, payload, parent, time.perf_counter() - started,
                                           settings=settings, blacklist_hash=blacklist_hash)
                    print(summary_text)
                return record
            return self.save(stage, payload, parent, time.perf_counter() - started,
                             settings=settings, blacklist_hash=blacklist_hash)


def run_stages(api, stages=(1, 2, 3, 4), *, state_dir=None):
    if state_dir is None:
        # Programmatic full scans keep their temporary state outside the repository.
        with tempfile.TemporaryDirectory(prefix="csrepo-stages-") as temporary:
            path = Path(temporary).resolve()
            if not path.is_relative_to(Path(tempfile.gettempdir()).resolve()):
                raise RuntimeError("Geçici çalışma klasörü geçersiz.")
            return run_stages(api, stages, state_dir=path)
    store = StageStore(api, state_dir)
    with work_lock(store.directory), api["timed_phase"]("Toplam tarama"), api["shared_plugin_lists"]():
        result = None
        for stage in stages:
            result = store.execute(stage)
    return result


def run_cli(api, argv=None):
    parser = argparse.ArgumentParser(description="CloudStream taramasını adım adım çalıştırın.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--stage", choices=("1", "2", "3", "4", "all"), help="Yalnızca seçilen aşamayı çalıştırır.")
    selection.add_argument("--from-stage", type=int, choices=range(1, 5), help="Bu aşamadan sona kadar devam eder.")
    selection.add_argument("--status", action="store_true", help="Kaydedilmiş aşamaları gösterir; tarama yapmaz.")
    parser.add_argument("--state-dir", type=Path, help="Ara sonuçlar için ayrı çalışma klasörü.")
    args = parser.parse_args(argv)
    directory = args.state_dir or api["ROOT"] / ".scan-state"
    try:
        if args.status:
            StageStore(api, directory).status()
            return
        stages = range(args.from_stage, 5) if args.from_stage else (
            (int(args.stage),) if args.stage and args.stage != "all" else range(1, 5)
        )
        return run_stages(api, stages, state_dir=directory)
    except (RuntimeError, OSError) as error:
        parser.exit(1, f"Hata: {error}\n")
