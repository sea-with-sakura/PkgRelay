from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tarfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import zstandard

from .config import Settings, load_settings
from .store import CacheStore


CONDA_IMPORT_SOURCE = "import-conda"
PIP_IMPORT_SOURCE = "import-pip"


class ImportError(ValueError):
    pass


@dataclass(frozen=True)
class ImportSummary:
    scanned: int = 0
    imported: int = 0
    skipped: int = 0
    platforms: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "imported": self.imported,
            "skipped": self.skipped,
            "platforms": list(self.platforms),
        }


@dataclass(frozen=True)
class PipImportSummary:
    scanned: int = 0
    imported: int = 0
    skipped: int = 0
    projects: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "imported": self.imported,
            "skipped": self.skipped,
            "projects": list(self.projects),
        }


def _read_index(archive: Path, filename: str | None = None) -> dict[str, Any]:
    name = filename or archive.name
    if name.endswith(".tar.bz2"):
        with tarfile.open(archive, "r:bz2") as package:
            handle = package.extractfile("info/index.json")
            if handle is None:
                raise ImportError(f"No info/index.json in {archive.name}")
            return json.load(handle)

    if name.endswith(".conda"):
        with zipfile.ZipFile(archive) as package:
            info_member = next(
                (name for name in package.namelist() if name.startswith("info-") and name.endswith(".tar.zst")),
                None,
            )
            if not info_member:
                raise ImportError(f"No info archive in {archive.name}")
            with package.open(info_member) as zipped:
                decompressor = zstandard.ZstdDecompressor()
                with decompressor.stream_reader(zipped) as stream:
                    with tarfile.open(fileobj=stream, mode="r|") as info_archive:
                        for member in info_archive:
                            if member.name == "info/index.json":
                                handle = info_archive.extractfile(member)
                                if handle is None:
                                    break
                                return json.load(handle)
        raise ImportError(f"No info/index.json in {archive.name}")

    raise ImportError(f"Unsupported package archive: {archive.name}")


def _platform(index: dict[str, Any]) -> str:
    if isinstance(index.get("subdir"), str):
        return index["subdir"]
    if index.get("noarch"):
        return "noarch"
    platform = index.get("platform", "linux")
    arch = index.get("arch", "64")
    return f"{platform}-{arch}"


def _validate_import_path(settings: Settings, package_cache: Path) -> Path:
    resolved = package_cache.resolve()
    if not resolved.is_dir():
        raise ImportError(f"Package cache directory does not exist: {resolved}")
    for root in settings.import_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ImportError(f"Import path is not in import_roots: {resolved}")


def _urls_by_filename(package_cache: Path) -> dict[str, str]:
    urls_file = package_cache / "urls.txt"
    if not urls_file.is_file():
        return {}
    urls: dict[str, str] = {}
    for line in urls_file.read_text(errors="replace").splitlines():
        url = line.strip()
        if url.startswith(("http://", "https://")):
            urls[Path(url.split("?", 1)[0]).name] = url
    return urls


def import_conda_cache(settings: Settings, package_cache: str | Path, dry_run: bool = False) -> ImportSummary:
    source_id = CONDA_IMPORT_SOURCE
    package_cache = _validate_import_path(settings, Path(package_cache))
    archives = sorted(
        path for path in package_cache.iterdir()
        if path.is_file() and (path.name.endswith(".conda") or path.name.endswith(".tar.bz2"))
    )
    urls = _urls_by_filename(package_cache)
    records: dict[str, dict[str, tuple[Path, dict[str, Any], str]]] = defaultdict(dict)
    for archive in archives:
        try:
            index = _read_index(archive)
            records[_platform(index)][archive.name] = (archive, index, urls.get(archive.name, ""))
        except (ImportError, OSError, tarfile.TarError, zipfile.BadZipFile, json.JSONDecodeError):
            continue

    store = CacheStore(
        settings.cache_dir,
        {**{name: source.pool for name, source in settings.sources.items()}, source_id: "conda"},
    )
    imported = skipped = 0
    try:
        for platform, packages in records.items():
            for filename, (archive, index, original_url) in packages.items():
                destination = f"{platform}/{filename}"
                existing = store.file_path(source_id, destination)
                if existing.is_file():
                    skipped += 1
                    continue
                if dry_run:
                    imported += 1
                    continue
                temporary_path = store.write_temp(source_id)
                try:
                    shutil.copyfile(archive, temporary_path)
                    sha256, size = store.publish(temporary_path, source_id, destination)
                finally:
                    temporary_path.unlink(missing_ok=True)
                store.upsert(
                    source_id=source_id,
                    path=destination,
                    upstream_url=original_url or f"file://{archive}",
                    final_url=f"file://{archive}",
                    sha256=sha256,
                    size=size,
                    content_type="application/octet-stream",
                    metadata=False,
                    etag=None,
                    last_modified=None,
                )
                imported += 1
    finally:
        store.close()
    return ImportSummary(
        scanned=len(archives),
        imported=imported,
        skipped=skipped,
        platforms=tuple(sorted(records)),
    )


_PIP_ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".tar.bz2", ".tar.xz", ".zip")


def _normalise_project(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _project_from_metadata(raw: bytes, archive: Path) -> str:
    name = BytesParser().parsebytes(raw).get("Name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ImportError(f"Invalid or missing project name in {archive.name}")
    return _normalise_project(name)


def _read_pip_project(archive: Path, filename: str | None = None) -> str:
    name = filename or archive.name
    if name.endswith(".whl") or name.endswith(".zip"):
        with zipfile.ZipFile(archive) as package:
            metadata = next(
                (
                    name for name in package.namelist()
                    if name.endswith(".dist-info/METADATA") or name.endswith("PKG-INFO")
                ),
                None,
            )
            if metadata:
                return _project_from_metadata(package.read(metadata), archive)
        raise ImportError(f"No package metadata in {archive.name}")
    with tarfile.open(archive, "r:*") as package:
        metadata = next((member for member in package if member.name.endswith("PKG-INFO")), None)
        if metadata is None:
            raise ImportError(f"No package metadata in {archive.name}")
        handle = package.extractfile(metadata)
        if handle is None:
            raise ImportError(f"Cannot read package metadata in {archive.name}")
        return _project_from_metadata(handle.read(), archive)


def _pip_origin_url(package_cache: Path, archive: Path, origin_root: str | None) -> str:
    if origin_root is None:
        return archive.resolve().as_uri()
    root = Path(origin_root)
    if not root.is_absolute():
        raise ImportError("origin_root must be an absolute host path")
    return (root / archive.relative_to(package_cache)).as_uri()


def import_pip_cache(
    settings: Settings,
    package_cache: str | Path,
    dry_run: bool = False,
    origin_root: str | None = None,
) -> PipImportSummary:
    source_id = PIP_IMPORT_SOURCE
    package_cache = _validate_import_path(settings, Path(package_cache))
    archives = sorted(
        path for path in package_cache.rglob("*")
        if path.is_file() and path.name.endswith(_PIP_ARCHIVE_SUFFIXES) and not path.is_symlink()
    )
    discovered: list[tuple[Path, str]] = []
    for archive in archives:
        try:
            discovered.append((archive, _read_pip_project(archive)))
        except (ImportError, OSError, tarfile.TarError, zipfile.BadZipFile):
            continue

    store = CacheStore(
        settings.cache_dir,
        {**{name: source.pool for name, source in settings.sources.items()}, source_id: "pip"},
    )
    imported = skipped = 0
    try:
        for archive, project in discovered:
            origin = _pip_origin_url(package_cache, archive, origin_root)
            identity = hashlib.sha256(origin.encode()).hexdigest()
            cache_path = f"imports/{identity}/{archive.name}"
            existing = store.file_path(source_id, cache_path)
            if existing.is_file():
                skipped += 1
                record = store.get(source_id, cache_path)
                if record and not dry_run:
                    store.upsert(
                        source_id=source_id,
                        path=cache_path,
                        upstream_url=origin,
                        final_url=origin,
                        sha256=record.sha256,
                        size=record.size,
                        content_type=record.content_type,
                        metadata=record.is_metadata,
                        etag=record.etag,
                        last_modified=record.last_modified,
                    )
            elif dry_run:
                imported += 1
                continue
            else:
                temporary_path = store.write_temp(source_id)
                try:
                    shutil.copyfile(archive, temporary_path)
                    sha256, size = store.publish(temporary_path, source_id, cache_path)
                finally:
                    temporary_path.unlink(missing_ok=True)
                store.upsert(
                    source_id=source_id,
                    path=cache_path,
                    upstream_url=origin,
                    final_url=origin,
                    sha256=sha256,
                    size=size,
                    content_type="application/octet-stream",
                    metadata=False,
                    etag=None,
                    last_modified=None,
                )
                imported += 1
            store.register_pypi_link(source_id, project, origin, cache_path, archive.name)
    finally:
        store.close()
    return PipImportSummary(
        scanned=len(archives),
        imported=imported,
        skipped=skipped,
        projects=tuple(sorted({project for _, project in discovered})),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a Conda or pip cache into the unified cache pool")
    parser.add_argument("package_cache", help="Path to an allowed package-cache directory")
    parser.add_argument("--kind", choices=("conda", "pypi"), default="conda")
    parser.add_argument("--dry-run", action="store_true", help="Inspect without copying files")
    parser.add_argument("--origin-root", help="Absolute original host directory for provenance")
    parser.add_argument("--config", help="Path to config.yaml")
    args = parser.parse_args()
    settings = load_settings(args.config)
    if args.kind == "pypi":
        summary = import_pip_cache(settings, args.package_cache, args.dry_run, args.origin_root)
    else:
        summary = import_conda_cache(settings, args.package_cache, args.dry_run)
    print(json.dumps(summary.as_dict()))


if __name__ == "__main__":
    main()
