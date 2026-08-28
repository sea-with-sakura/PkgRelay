from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator


class UnsafePathError(ValueError):
    pass


def normalise_path(request_path: str) -> str:
    path = PurePosixPath(request_path)
    if not request_path or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise UnsafePathError("Invalid package path")
    return path.as_posix()


def is_metadata_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        name == "channeldata.json"
        or name in {"notices.json", "terms.json"}
        or name.startswith("repodata")
        or name.startswith("current_repodata")
        or name.endswith(".jlap")
        or name.endswith(".msgpack.zst")
    )


@dataclass(frozen=True)
class ArtifactRecord:
    source_id: str
    pool: str
    path: str
    upstream_url: str
    final_url: str
    sha256: str
    size: int
    content_type: str | None
    is_metadata: bool
    fetched_at: float
    last_accessed_at: float
    hit_count: int
    etag: str | None
    last_modified: str | None


class CacheStore:
    """Content-addressed storage with exactly two pools: conda and pip.

    ``source_id`` is only an upstream-route and provenance key; it never
    creates a separate cache directory.
    """

    VALID_POOLS = frozenset({"conda", "pip"})

    def __init__(self, root: Path, source_pools: dict[str, str] | None = None):
        self.root = root
        self.files_root = root / "files"
        self.tmp_root = root / "tmp"
        self.source_pools = dict(source_pools or {})
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_root.mkdir(exist_ok=True)
        for pool in self.VALID_POOLS:
            (self.files_root / pool / "blobs").mkdir(parents=True, exist_ok=True)
        self.tmp_root.mkdir(exist_ok=True)
        self.db = sqlite3.connect(root / "metadata.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._published_paths: dict[tuple[str, str], Path] = {}
        self.db.execute("PRAGMA journal_mode=WAL")
        # Package archives are already atomically published before their
        # metadata is committed.  Hit/usage counters may lose only the most
        # recent transaction after a power loss, so avoid an HDD fsync on
        # every cache hit while retaining WAL consistency.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                source_id TEXT NOT NULL,
                pool TEXT NOT NULL DEFAULT 'conda',
                path TEXT NOT NULL,
                upstream_url TEXT NOT NULL,
                final_url TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                content_type TEXT,
                is_metadata INTEGER NOT NULL,
                fetched_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                etag TEXT,
                last_modified TEXT,
                PRIMARY KEY (source_id, path)
            );
            CREATE TABLE IF NOT EXISTS upstream_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                path TEXT NOT NULL,
                upstream_url TEXT NOT NULL,
                status_code INTEGER,
                error TEXT,
                attempted_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS artifacts_access_idx
              ON artifacts(last_accessed_at);
            CREATE TABLE IF NOT EXISTS pypi_links (
                source_id TEXT NOT NULL,
                token TEXT NOT NULL,
                project TEXT NOT NULL,
                upstream_url TEXT NOT NULL,
                cache_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                PRIMARY KEY (source_id, token)
            );
            CREATE INDEX IF NOT EXISTS pypi_links_project_idx
              ON pypi_links(source_id, project);
            CREATE TABLE IF NOT EXISTS dynamic_pypi_sources (
                source_id TEXT PRIMARY KEY,
                upstream_url TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                last_seen_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                machine TEXT NOT NULL,
                username TEXT NOT NULL,
                client_version TEXT,
                last_ip TEXT NOT NULL,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS client_usage_daily (
                day TEXT NOT NULL,
                client_id TEXT NOT NULL,
                downloads INTEGER NOT NULL DEFAULT 0,
                bytes_served INTEGER NOT NULL DEFAULT 0,
                cache_hits INTEGER NOT NULL DEFAULT 0,
                bytes_saved INTEGER NOT NULL DEFAULT 0,
                cache_misses INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, client_id),
                FOREIGN KEY (client_id) REFERENCES clients(client_id)
            );
            CREATE INDEX IF NOT EXISTS client_usage_daily_client_idx
              ON client_usage_daily(client_id, day);
            """
        )
        self.db.commit()
        columns = {str(row["name"]) for row in self.db.execute("PRAGMA table_info(artifacts)")}
        if "pool" not in columns:
            self.db.execute("ALTER TABLE artifacts ADD COLUMN pool TEXT NOT NULL DEFAULT 'conda'")
            self.db.commit()
        client_columns = {
            str(row["name"]) for row in self.db.execute("PRAGMA table_info(clients)")
        }
        if "client_version" not in client_columns:
            self.db.execute("ALTER TABLE clients ADD COLUMN client_version TEXT")
            self.db.commit()
        # These Conda control documents are metadata too. Older cache records
        # may predate their classification, so correct them on open.
        self.db.execute(
            "UPDATE artifacts SET is_metadata = 1 WHERE path IN ('notices.json', 'terms.json')"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def pool_for_source(self, source_id: str) -> str:
        pool = self.source_pools.get(source_id)
        if pool is None:
            pool = "pip" if source_id in {"pypi", "import-pip"} or source_id.startswith("external-") else "conda"
        if pool not in self.VALID_POOLS:
            raise ValueError(f"Invalid cache pool for {source_id!r}: {pool!r}")
        return pool

    def blob_path(self, pool: str, sha256: str) -> Path:
        if pool not in self.VALID_POOLS:
            raise ValueError(f"Invalid cache pool: {pool!r}")
        return self.files_root / pool / "blobs" / sha256[:2] / sha256

    def file_path(self, source_id: str, path: str) -> Path:
        record = self.get(source_id, path)
        if record:
            blob = self.blob_path(record.pool, record.sha256)
            if blob.is_file():
                return blob
        pending = self._published_paths.get((source_id, normalise_path(path)))
        if pending and pending.is_file():
            return pending
        return self.files_root / self.pool_for_source(source_id) / ".missing" / normalise_path(path)

    def get(self, source_id: str, path: str) -> ArtifactRecord | None:
        row = self.db.execute(
            "SELECT * FROM artifacts WHERE source_id = ? AND path = ?", (source_id, path)
        ).fetchone()
        return ArtifactRecord(**dict(row)) if row else None

    def find_by_upstream_url(
        self, pool: str, upstream_url: str, *, metadata: bool = False
    ) -> ArtifactRecord | None:
        """Return an artifact already registered for the exact upstream URL.

        This is the only cross-source reuse rule. A filename alone is not an
        identity: distinct channels can legitimately publish different bytes
        under the same filename.  Metadata is eligible too, but callers still
        apply its normal TTL and conditionally revalidate stale copies.
        """
        row = self.db.execute(
            """SELECT * FROM artifacts
               WHERE pool = ? AND is_metadata = ?
                 AND (upstream_url = ? OR final_url = ?)
               ORDER BY last_accessed_at DESC LIMIT 1""",
            (pool, int(metadata), upstream_url, upstream_url),
        ).fetchone()
        return ArtifactRecord(**dict(row)) if row else None

    def ensure_dynamic_pypi_source(self, upstream_url: str) -> sqlite3.Row:
        source_id = "external-" + hashlib.sha256(upstream_url.encode()).hexdigest()[:24]
        now = time.time()
        self.db.execute(
            """INSERT INTO dynamic_pypi_sources (source_id, upstream_url, created_at, last_seen_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(upstream_url) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (source_id, upstream_url, now, now),
        )
        self.db.commit()
        return self.db.execute(
            "SELECT * FROM dynamic_pypi_sources WHERE upstream_url = ?", (upstream_url,)
        ).fetchone()

    def get_dynamic_pypi_source(self, source_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM dynamic_pypi_sources WHERE source_id = ?", (source_id,)
        ).fetchone()

    def iter_dynamic_pypi_sources(self) -> Iterator[sqlite3.Row]:
        yield from self.db.execute(
            "SELECT * FROM dynamic_pypi_sources ORDER BY last_seen_at DESC"
        ).fetchall()

    def touch(self, source_id: str, path: str) -> None:
        self.db.execute(
            """UPDATE artifacts SET hit_count = hit_count + 1, last_accessed_at = ?
               WHERE source_id = ? AND path = ?""",
            (time.time(), source_id, path),
        )
        self.db.commit()

    def record_attempt(
        self, source_id: str, path: str, upstream_url: str, status_code: int | None, error: str | None
    ) -> None:
        self.db.execute(
            """INSERT INTO upstream_attempts
               (source_id, path, upstream_url, status_code, error, attempted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source_id, path, upstream_url, status_code, error, time.time()),
        )
        self.db.commit()

    def upsert(
        self,
        *,
        source_id: str,
        path: str,
        upstream_url: str,
        final_url: str,
        sha256: str,
        size: int,
        content_type: str | None,
        metadata: bool,
        etag: str | None,
        last_modified: str | None,
    ) -> ArtifactRecord:
        now = time.time()
        pool = self.pool_for_source(source_id)
        self.db.execute(
            """INSERT INTO artifacts
               (source_id, pool, path, upstream_url, final_url, sha256, size, content_type,
                is_metadata, fetched_at, last_accessed_at, hit_count, etag, last_modified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
               ON CONFLICT(source_id, path) DO UPDATE SET
                 pool=excluded.pool, upstream_url=excluded.upstream_url, final_url=excluded.final_url,
                 sha256=excluded.sha256, size=excluded.size, content_type=excluded.content_type,
                 is_metadata=excluded.is_metadata, fetched_at=excluded.fetched_at,
                 last_accessed_at=excluded.last_accessed_at, etag=excluded.etag,
                 last_modified=excluded.last_modified""",
            (
                source_id,
                pool,
                path,
                upstream_url,
                final_url,
                sha256,
                size,
                content_type,
                int(metadata),
                now,
                now,
                etag,
                last_modified,
            ),
        )
        self.db.commit()
        return self.get(source_id, path)  # type: ignore[return-value]

    def refresh_not_modified(self, source_id: str, path: str) -> ArtifactRecord | None:
        now = time.time()
        self.db.execute(
            "UPDATE artifacts SET fetched_at = ?, last_accessed_at = ? WHERE source_id = ? AND path = ?",
            (now, now, source_id, path),
        )
        self.db.commit()
        return self.get(source_id, path)

    def write_temp(self, source_id: str) -> Path:
        source_tmp = self.tmp_root / self.pool_for_source(source_id)
        source_tmp.mkdir(parents=True, exist_ok=True)
        return source_tmp / f"download-{os.urandom(12).hex()}.part"

    def publish(self, temporary_path: Path, source_id: str, path: str) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with temporary_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        sha256 = digest.hexdigest()
        target = self.blob_path(self.pool_for_source(source_id), sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            temporary_path.unlink(missing_ok=True)
        else:
            os.replace(temporary_path, target)
        self._published_paths[(source_id, normalise_path(path))] = target
        return sha256, size

    def store_bytes(
        self,
        *,
        source_id: str,
        path: str,
        content: bytes,
        upstream_url: str,
        metadata: bool,
        content_type: str = "application/json",
    ) -> ArtifactRecord:
        temporary_path = self.write_temp(source_id)
        try:
            temporary_path.write_bytes(content)
            sha256, size = self.publish(temporary_path, source_id, path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return self.upsert(
            source_id=source_id,
            path=path,
            upstream_url=upstream_url,
            final_url=upstream_url,
            sha256=sha256,
            size=size,
            content_type=content_type,
            metadata=metadata,
            etag=None,
            last_modified=None,
        )

    def iter_recent(self, limit: int = 100) -> Iterator[ArtifactRecord]:
        rows = self.db.execute(
            "SELECT * FROM artifacts ORDER BY last_accessed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        for row in rows:
            yield ArtifactRecord(**dict(row))

    def search_artifacts(
        self,
        source_id: str,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
        include_metadata: bool = True,
    ) -> tuple[int, list[ArtifactRecord]]:
        where = "source_id = ?"
        values: list[object] = [source_id]
        if not include_metadata:
            where += " AND is_metadata = 0"
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where += " AND (path LIKE ? ESCAPE '\\' OR upstream_url LIKE ? ESCAPE '\\' OR sha256 LIKE ? ESCAPE '\\')"
            values.extend([f"%{escaped}%"] * 3)
        total = self.db.execute(f"SELECT COUNT(*) FROM artifacts WHERE {where}", values).fetchone()[0]
        rows = self.db.execute(
            f"""SELECT * FROM artifacts WHERE {where}
                ORDER BY last_accessed_at DESC LIMIT ? OFFSET ?""",
            [*values, limit, offset],
        ).fetchall()
        return total, [ArtifactRecord(**dict(row)) for row in rows]

    def search_pool_artifacts(
        self, pool: str, query: str = "", limit: int = 100, offset: int = 0
    ) -> tuple[int, list[ArtifactRecord]]:
        if pool not in self.VALID_POOLS:
            raise ValueError(f"Invalid cache pool: {pool!r}")
        # Pool browsers are package browsers.  Conda repodata and PyPI
        # ``simple`` HTML are transport metadata, not installable archives.
        where = "pool = ? AND is_metadata = 0"
        values: list[object] = [pool]
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where += " AND (path LIKE ? ESCAPE '\\' OR upstream_url LIKE ? ESCAPE '\\' OR sha256 LIKE ? ESCAPE '\\')"
            values.extend([f"%{escaped}%"] * 3)
        total = self.db.execute(f"SELECT COUNT(*) FROM artifacts WHERE {where}", values).fetchone()[0]
        rows = self.db.execute(
            f"""SELECT * FROM artifacts WHERE {where}
                ORDER BY last_accessed_at DESC LIMIT ? OFFSET ?""",
            [*values, limit, offset],
        ).fetchall()
        return total, [ArtifactRecord(**dict(row)) for row in rows]

    def search_pool_blobs(
        self, pool: str, query: str = "", limit: int = 100, offset: int = 0
    ) -> tuple[int, int, list[dict[str, object]]]:
        """Search the physical objects in a pool, not their routing records.

        A single content-addressed Blob can be referenced by multiple source
        routes (for example a legacy static Conda route and its newer dynamic
        counterpart).  The dashboard's pool browser should show that Blob once
        while retaining route counts for provenance and later cache eviction.
        """
        if pool not in self.VALID_POOLS:
            raise ValueError(f"Invalid cache pool: {pool!r}")
        # The pool UI shows installable archives only.  Index documents stay
        # cached for pip/Conda, but do not appear as package files.
        where = "pool = ? AND is_metadata = 0"
        values: list[object] = [pool]
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where += " AND (path LIKE ? ESCAPE '\\' OR upstream_url LIKE ? ESCAPE '\\' OR sha256 LIKE ? ESCAPE '\\')"
            values.extend([f"%{escaped}%"] * 3)
        route_total = self.db.execute(
            f"SELECT COUNT(*) FROM artifacts WHERE {where}", values
        ).fetchone()[0]
        total = self.db.execute(
            f"SELECT COUNT(*) FROM (SELECT sha256 FROM artifacts WHERE {where} GROUP BY sha256)",
            values,
        ).fetchone()[0]
        rows = self.db.execute(
            f"""SELECT sha256, MIN(path) AS path, MAX(size) AS size,
                       SUM(hit_count) AS hit_count, MAX(fetched_at) AS fetched_at,
                       MAX(last_accessed_at) AS last_accessed_at,
                       MIN(upstream_url) AS upstream_url,
                       COUNT(*) AS route_count, COUNT(DISTINCT source_id) AS source_count
                FROM artifacts WHERE {where}
                GROUP BY sha256
                ORDER BY MAX(last_accessed_at) DESC LIMIT ? OFFSET ?""",
            [*values, limit, offset],
        ).fetchall()
        return total, route_total, [dict(row) for row in rows]

    def register_pypi_link(
        self, source_id: str, project: str, upstream_url: str, cache_path: str, filename: str
    ) -> str:
        token = self.pypi_link_token(source_id, upstream_url)
        self.register_pypi_links(((source_id, project, upstream_url, cache_path, filename),))
        return token

    @staticmethod
    def pypi_link_token(source_id: str, upstream_url: str) -> str:
        return hashlib.sha256(f"{source_id}\0{upstream_url}".encode()).hexdigest()

    def register_pypi_links(
        self, links: Iterable[tuple[str, str, str, str, str]]
    ) -> None:
        """Register a complete PEP 503 page in one SQLite transaction."""
        rows = [
            (source_id, self.pypi_link_token(source_id, upstream_url), project, upstream_url, cache_path, filename)
            for source_id, project, upstream_url, cache_path, filename in links
        ]
        if not rows:
            return
        self.db.executemany(
            """INSERT INTO pypi_links (source_id, token, project, upstream_url, cache_path, filename)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id, token) DO UPDATE SET
               project=excluded.project, cache_path=excluded.cache_path, filename=excluded.filename""",
            rows,
        )
        self.db.commit()

    def get_pypi_link(self, source_id: str, token: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM pypi_links WHERE source_id = ? AND token = ?", (source_id, token)
        ).fetchone()

    def iter_pypi_links(self, source_id: str, project: str) -> Iterator[sqlite3.Row]:
        rows = self.db.execute(
            """SELECT * FROM pypi_links WHERE source_id = ? AND project = ?
               ORDER BY filename""",
            (source_id, project),
        ).fetchall()
        yield from rows

    def iter_imported_pypi_links(self, project: str) -> Iterator[sqlite3.Row]:
        rows = self.db.execute(
            """SELECT * FROM pypi_links WHERE source_id = 'import-pip' AND project = ?
               ORDER BY filename""",
            (project,),
        ).fetchall()
        yield from rows

    def register_client(
        self,
        client_id: str,
        machine: str,
        username: str,
        client_version: str,
        ip_address: str,
    ) -> None:
        """Register the opaque client id used to attribute package downloads."""
        now = time.time()
        self.db.execute(
            """INSERT INTO clients
               (client_id, machine, username, client_version, last_ip, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(client_id) DO UPDATE SET
                 machine=excluded.machine, username=excluded.username,
                 client_version=excluded.client_version,
                 last_ip=excluded.last_ip, last_seen_at=excluded.last_seen_at""",
            (client_id, machine, username, client_version, ip_address, now, now),
        )
        self.db.commit()

    def registered_client_version(self, client_id: str) -> str | None:
        row = self.db.execute(
            "SELECT client_version FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        if row is None or row["client_version"] is None:
            return None
        return str(row["client_version"])

    def record_client_download(
        self, client_id: str, *, size: int, cache_status: str, ip_address: str
    ) -> None:
        """Add one installable archive transfer to the daily client total.

        ``bytes_saved`` deliberately counts only archive bytes served from an
        existing central object (HIT or REUSED).  A first MISS still reaches
        the client, but it did not avoid an upstream transfer.
        """
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        hit = int(cache_status in {"HIT", "REUSED"})
        self.db.execute(
            "UPDATE clients SET last_ip = ?, last_seen_at = ? WHERE client_id = ?",
            (ip_address, now, client_id),
        )
        self.db.execute(
            """INSERT INTO client_usage_daily
               (day, client_id, downloads, bytes_served, cache_hits, bytes_saved, cache_misses)
               VALUES (?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(day, client_id) DO UPDATE SET
                 downloads=downloads + 1,
                 bytes_served=bytes_served + excluded.bytes_served,
                 cache_hits=cache_hits + excluded.cache_hits,
                 bytes_saved=bytes_saved + excluded.bytes_saved,
                 cache_misses=cache_misses + excluded.cache_misses""",
            (day, client_id, max(size, 0), hit, max(size, 0) * hit, 1 - hit),
        )
        self.db.commit()

    def client_usage(self, days: int = 30) -> dict[str, object]:
        """Return aggregated usage for the dashboard without per-file logs."""
        days = min(max(days, 1), 3650)
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
        totals = self.db.execute(
            """SELECT COUNT(DISTINCT client_id) AS client_count,
                      COALESCE(SUM(downloads), 0) AS downloads,
                      COALESCE(SUM(bytes_served), 0) AS bytes_served,
                      COALESCE(SUM(cache_hits), 0) AS cache_hits,
                      COALESCE(SUM(bytes_saved), 0) AS bytes_saved
               FROM client_usage_daily WHERE day >= ?""",
            (since,),
        ).fetchone()
        clients = self.db.execute(
            """SELECT c.machine, c.username, c.client_version, c.last_ip, c.last_seen_at,
                      SUM(u.downloads) AS downloads, SUM(u.bytes_served) AS bytes_served,
                      SUM(u.cache_hits) AS cache_hits, SUM(u.bytes_saved) AS bytes_saved
               FROM client_usage_daily u JOIN clients c ON c.client_id = u.client_id
               WHERE u.day >= ?
               GROUP BY u.client_id
               ORDER BY bytes_served DESC, c.last_seen_at DESC""",
            (since,),
        ).fetchall()
        machine_count = len({str(row["machine"]) for row in clients})
        return {
            "days": days,
            "totals": {**dict(totals), "machine_count": machine_count},
            "clients": [dict(row) for row in clients],
        }

    def statistics(self) -> dict[str, object]:
        totals = self.db.execute(
            """SELECT COUNT(*) AS artifact_count, COALESCE(SUM(hit_count), 0) AS total_hits
               FROM artifacts"""
        ).fetchone()
        blobs = self.db.execute(
            """SELECT COUNT(*) AS blob_count, COALESCE(SUM(size), 0) AS total_bytes
               FROM (SELECT pool, sha256, MAX(size) AS size FROM artifacts GROUP BY pool, sha256)"""
        ).fetchone()
        pools: list[dict[str, object]] = []
        for pool in sorted(self.VALID_POOLS):
            usage = self.db.execute(
                """SELECT COUNT(*) AS artifact_count, COALESCE(SUM(hit_count), 0) AS total_hits
                   FROM artifacts WHERE pool = ? AND is_metadata = 0""",
                (pool,),
            ).fetchone()
            blob_usage = self.db.execute(
                """SELECT COUNT(*) AS blob_count, COALESCE(SUM(size), 0) AS total_bytes
                   FROM (SELECT sha256, MAX(size) AS size FROM artifacts
                         WHERE pool = ? AND is_metadata = 0 GROUP BY sha256)""",
                (pool,),
            ).fetchone()
            pools.append({"pool": pool, **dict(usage), **dict(blob_usage)})
        sources = self.db.execute(
            """SELECT source_id, COUNT(*) AS artifact_count,
                      COALESCE(SUM(CASE WHEN is_metadata = 0 THEN 1 ELSE 0 END), 0) AS package_count,
                      COALESCE(SUM(CASE WHEN is_metadata = 1 THEN 1 ELSE 0 END), 0) AS metadata_count,
                      COALESCE(SUM(size), 0) AS total_bytes,
                      COALESCE(SUM(CASE WHEN is_metadata = 0 THEN size ELSE 0 END), 0) AS package_bytes,
                      COALESCE(SUM(hit_count), 0) AS total_hits
               FROM artifacts GROUP BY source_id ORDER BY source_id"""
        ).fetchall()
        return {
            "artifact_count": totals["artifact_count"],
            "blob_count": blobs["blob_count"],
            "total_bytes": blobs["total_bytes"],
            "total_hits": totals["total_hits"],
            "pools": pools,
            "sources": [dict(row) for row in sources],
        }
