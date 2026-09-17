from __future__ import annotations

import hashlib
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from fastapi import Request

from .config import Settings
from .importer import (
    CONDA_IMPORT_SOURCE,
    PIP_IMPORT_SOURCE,
    ImportError as ArchiveImportError,
    _read_index,
    _read_pip_project,
)
from .store import CacheStore, normalise_path


class SyncError(ValueError):
    pass


@dataclass(frozen=True)
class SyncRecord:
    pool: str
    source_id: str
    path: str
    sha256: str
    size: int


class ClientSync:
    """Register or receive package archives supplied by trusted LAN clients."""

    def __init__(self, settings: Settings, store: CacheStore):
        self.settings = settings
        self.store = store

    @staticmethod
    def validate(kind: str, sha256: str, filename: str, origin: str) -> None:
        if kind not in {"conda", "pip"}:
            raise SyncError("kind must be conda or pip")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise SyncError("Invalid SHA256")
        candidate = PurePosixPath(filename)
        if not filename or candidate.name != filename or filename in {".", ".."}:
            raise SyncError("Invalid filename")
        suffixes = (".conda", ".tar.bz2") if kind == "conda" else (
            ".whl", ".tar.gz", ".tar.bz2", ".tar.xz", ".zip"
        )
        if not filename.endswith(suffixes):
            raise SyncError("Unsupported package archive")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https", "file"} or not parsed.path:
                raise SyncError("Invalid origin URL")

    def _conda_destination(
        self, archive: Path, filename: str, origin: str
    ) -> tuple[str, str, str, str]:
        try:
            _read_index(archive, filename)
        except (ArchiveImportError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise SyncError("Invalid Conda package archive") from exc
        parsed_origin = urlsplit(origin)
        gateway_parts = parsed_origin.path.strip("/").split("/")
        if len(gateway_parts) >= 3 and gateway_parts[0] == "get":
            source = self.settings.sources.get(gateway_parts[1])
            if source and source.kind == "conda":
                path = normalise_path("/".join(gateway_parts[2:]))
                if PurePosixPath(path).name == filename:
                    upstream = source.upstreams[0]
                    return source.name, path, upstream, f"{upstream}/{path}"
        for source in self.settings.sources.values():
            if source.kind != "conda":
                continue
            for upstream in source.upstreams:
                prefix = f"{upstream}/"
                if origin.startswith(prefix):
                    path = normalise_path(origin[len(prefix):].split("?", 1)[0])
                    if PurePosixPath(path).name == filename:
                        return source.name, path, upstream, origin
        raise SyncError("Conda archive origin is not a configured PkgRelay source")

    def _pip_destination(
        self, archive: Path, filename: str, sha256: str, origin: str
    ) -> tuple[str, str, str, str]:
        try:
            _read_pip_project(archive, filename)  # Validate before accepting a pip archive.
        except (ArchiveImportError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise SyncError("Invalid pip package archive") from exc
        value = origin or f"file:///client-sync/{sha256}/{filename}"
        return PIP_IMPORT_SOURCE, f"imports/{sha256}/{filename}", value, value

    def register(self, kind: str, sha256: str, filename: str, origin: str) -> SyncRecord | None:
        self.validate(kind, sha256, filename, origin)
        pool = "conda" if kind == "conda" else "pip"
        archive = self.store.blob_path(pool, sha256)
        if not archive.is_file():
            return None
        if kind == "conda":
            source_id, path, upstream_url, final_url = self._conda_destination(archive, filename, origin)
        else:
            source_id, path, upstream_url, final_url = self._pip_destination(archive, filename, sha256, origin)
        record = self.store.upsert(
            source_id=source_id,
            path=path,
            upstream_url=upstream_url,
            final_url=final_url,
            sha256=sha256,
            size=archive.stat().st_size,
            content_type="application/octet-stream",
            metadata=False,
            etag=None,
            last_modified=None,
        )
        if kind == "pip":
            try:
                project = _read_pip_project(archive, filename)
            except (ArchiveImportError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
                raise SyncError("Invalid pip package archive") from exc
            self.store.register_pypi_link(PIP_IMPORT_SOURCE, project, final_url, path, filename)
        return SyncRecord(record.pool, record.source_id, record.path, sha256, record.size)

    async def upload(
        self, request: Request, kind: str, sha256: str, filename: str, origin: str
    ) -> SyncRecord:
        self.validate(kind, sha256, filename, origin)
        pool = "conda" if kind == "conda" else "pip"
        temporary_path = self.store.write_temp(CONDA_IMPORT_SOURCE if kind == "conda" else PIP_IMPORT_SOURCE)
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary_path.open("wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > self.settings.client_sync_max_upload_bytes:
                        raise SyncError("Upload exceeds configured size limit")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != sha256:
                raise SyncError("Uploaded SHA256 does not match")
            destination = self.store.blob_path(pool, sha256)
            if not destination.is_file():
                self.store.publish(
                    temporary_path,
                    CONDA_IMPORT_SOURCE if kind == "conda" else PIP_IMPORT_SOURCE,
                    f"uploads/{sha256}/{filename}",
                )
            record = self.register(kind, sha256, filename, origin)
            if record is None:
                raise SyncError("Failed to publish uploaded package")
            return record
        finally:
            temporary_path.unlink(missing_ok=True)
