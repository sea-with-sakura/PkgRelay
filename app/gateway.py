from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .config import SourceConfig
from .store import ArtifactRecord, CacheStore, is_metadata_path, normalise_path


class UpstreamUnavailable(Exception):
    pass


class PackageNotFound(Exception):
    pass


@dataclass(frozen=True)
class CacheResult:
    file_path: Path
    record: ArtifactRecord
    cache_status: str


@dataclass
class StreamingCacheResult:
    """A cache miss whose upstream bytes are being forwarded immediately.

    The body writes to a temporary file while it yields chunks to the client.
    Once the upstream response is complete, that file is atomically published
    to the content-addressed cache.
    """

    body: AsyncIterator[bytes]
    content_type: str | None
    content_length: int | None
    source_id: str
    cache_status: str = "MISS"


class Gateway:
    def __init__(self, store: CacheStore):
        self.store = store
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, source_id: str, path: str) -> asyncio.Lock:
        key = (source_id, path)
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _fresh(record: ArtifactRecord, metadata: bool, source: SourceConfig) -> bool:
        return not metadata or time.time() - record.fetched_at < source.metadata_ttl_seconds

    async def get(
        self,
        source: SourceConfig,
        requested_path: str,
        metadata: bool | None = None,
        remote_path: str | None = None,
    ) -> CacheResult:
        path = normalise_path(requested_path)
        metadata = is_metadata_path(path) if metadata is None else metadata
        target = self.store.file_path(source.name, path)
        record = self.store.get(source.name, path)
        if target.is_file() and record and self._fresh(record, metadata, source):
            self.store.touch(source.name, path)
            return CacheResult(target, self.store.get(source.name, path), "HIT")  # type: ignore[arg-type]

        lock = await self._lock_for(source.name, path)
        async with lock:
            record = self.store.get(source.name, path)
            if target.is_file() and record and self._fresh(record, metadata, source):
                self.store.touch(source.name, path)
                return CacheResult(target, self.store.get(source.name, path), "HIT")  # type: ignore[arg-type]
            upstream_path = remote_path or path
            urls = tuple(
                (urljoin(f"{upstream}/", upstream_path), upstream) for upstream in source.upstreams
            )
            reused = self._reuse_by_url(source, path, metadata, urls)
            if reused:
                if self._fresh(reused.record, metadata, source):
                    return reused
                # A matching blob can be reused across a legacy/static source
                # and a dynamically mapped Channel.  Metadata still honours
                # its TTL: retain the ETag/Last-Modified record and revalidate
                # it below when that shared copy is stale.
                record = reused.record
                target = reused.file_path
            return await self._fetch(source, path, metadata, record, target, urls)

    async def get_url(
        self, source: SourceConfig, requested_path: str, upstream_url: str, metadata: bool = False
    ) -> CacheResult:
        path = normalise_path(requested_path)
        target = self.store.file_path(source.name, path)
        record = self.store.get(source.name, path)
        if target.is_file() and record and self._fresh(record, metadata, source):
            self.store.touch(source.name, path)
            return CacheResult(target, self.store.get(source.name, path), "HIT")  # type: ignore[arg-type]
        lock = await self._lock_for(source.name, path)
        async with lock:
            record = self.store.get(source.name, path)
            if target.is_file() and record and self._fresh(record, metadata, source):
                self.store.touch(source.name, path)
                return CacheResult(target, self.store.get(source.name, path), "HIT")  # type: ignore[arg-type]
            reused = self._reuse_by_url(source, path, metadata, ((upstream_url, upstream_url),))
            if reused:
                return reused
            return await self._fetch(
                source, path, metadata, record, target, ((upstream_url, upstream_url),)
            )

    async def get_url_streaming(
        self, source: SourceConfig, requested_path: str, upstream_url: str
    ) -> CacheResult | StreamingCacheResult:
        """Get a non-metadata URL, streaming an uncached upstream response.

        ``get_url`` deliberately remains store-and-forward for callers that
        need a local file before responding (for example PEP 503 metadata).
        Package downloads use this method so pip receives bytes immediately,
        rather than timing out while a multi-gigabyte wheel is first cached.
        """
        path = normalise_path(requested_path)
        target = self.store.file_path(source.name, path)
        record = self.store.get(source.name, path)
        if target.is_file() and record:
            self.store.touch(source.name, path)
            return CacheResult(target, self.store.get(source.name, path), "HIT")  # type: ignore[arg-type]

        lock = await self._lock_for(source.name, path)
        await lock.acquire()
        try:
            record = self.store.get(source.name, path)
            target = self.store.file_path(source.name, path)
            if target.is_file() and record:
                self.store.touch(source.name, path)
                return CacheResult(target, self.store.get(source.name, path), "HIT")  # type: ignore[arg-type]
            reused = self._reuse_by_url(source, path, False, ((upstream_url, upstream_url),))
            if reused:
                return reused
            return await self._open_stream(source, path, upstream_url, lock)
        except BaseException:
            lock.release()
            raise

    async def get_streaming(
        self, source: SourceConfig, requested_path: str
    ) -> CacheResult | StreamingCacheResult:
        """Stream an uncached Conda package while publishing it to the cache.

        Metadata intentionally continues through :meth:`get`, where the full
        response is needed for conditional revalidation.  Package archives do
        not need that delay, so a cold request receives bytes immediately.
        """
        path = normalise_path(requested_path)
        target = self.store.file_path(source.name, path)
        record = self.store.get(source.name, path)
        if target.is_file() and record:
            self.store.touch(source.name, path)
            return CacheResult(target, self.store.get(source.name, path), "HIT")  # type: ignore[arg-type]

        lock = await self._lock_for(source.name, path)
        await lock.acquire()
        try:
            record = self.store.get(source.name, path)
            target = self.store.file_path(source.name, path)
            if target.is_file() and record:
                self.store.touch(source.name, path)
                return CacheResult(target, self.store.get(source.name, path), "HIT")  # type: ignore[arg-type]
            urls = tuple((urljoin(f"{upstream}/", path), upstream) for upstream in source.upstreams)
            reused = self._reuse_by_url(source, path, False, urls)
            if reused:
                return reused
            failures: list[str] = []
            for upstream_url, upstream in urls:
                try:
                    return await self._open_stream(source, path, upstream_url, lock)
                except PackageNotFound:
                    if not source.fallback_on_not_found:
                        raise
                    failures.append(f"{upstream}: HTTP 404")
                except UpstreamUnavailable as exc:
                    failures.append(str(exc))
            if failures and all(item.endswith("HTTP 404") for item in failures):
                raise PackageNotFound(path)
            raise UpstreamUnavailable("; ".join(failures) or "No upstream was reachable")
        except BaseException:
            lock.release()
            raise

    async def _open_stream(
        self, source: SourceConfig, path: str, upstream_url: str, lock: asyncio.Lock
    ) -> StreamingCacheResult:
        timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        response: httpx.Response | None = None
        try:
            request = client.build_request("GET", upstream_url)
            response = await client.send(request, stream=True)
            self.store.record_attempt(source.name, path, upstream_url, response.status_code, None)
            if response.status_code == 404:
                raise PackageNotFound(path)
            if response.status_code >= 500:
                raise UpstreamUnavailable(f"{upstream_url}: HTTP {response.status_code}")
            if response.status_code != 200:
                raise UpstreamUnavailable(f"{upstream_url}: HTTP {response.status_code}")
            temporary_path = self.store.write_temp(source.name)
            content_length: int | None = None
            try:
                content_length = int(response.headers["content-length"])
            except (KeyError, ValueError):
                pass
            return StreamingCacheResult(
                body=self._stream_to_cache(
                    source, path, upstream_url, client, response, temporary_path, lock
                ),
                content_type=response.headers.get("content-type"),
                content_length=content_length,
                source_id=source.name,
            )
        except httpx.HTTPError as exc:
            self.store.record_attempt(source.name, path, upstream_url, None, str(exc))
            raise UpstreamUnavailable(f"{upstream_url}: {exc}") from exc
        except BaseException:
            if response is not None:
                await response.aclose()
            await client.aclose()
            raise

    async def _stream_to_cache(
        self,
        source: SourceConfig,
        path: str,
        upstream_url: str,
        client: httpx.AsyncClient,
        response: httpx.Response,
        temporary_path: Path,
        lock: asyncio.Lock,
    ) -> AsyncIterator[bytes]:
        completed = False
        try:
            with temporary_path.open("wb") as output:
                # Keep HTTPX's 64 KiB default chunk size.  A larger explicit
                # chunk size delays the first byte until that whole buffer is
                # filled, which defeats streaming for slow upstreams.
                async for chunk in response.aiter_bytes():
                    output.write(chunk)
                    yield chunk
            sha256, size = self.store.publish(temporary_path, source.name, path)
            self.store.upsert(
                source_id=source.name,
                path=path,
                upstream_url=upstream_url,
                final_url=str(response.url),
                sha256=sha256,
                size=size,
                content_type=response.headers.get("content-type"),
                metadata=False,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )
            completed = True
        finally:
            if not completed:
                temporary_path.unlink(missing_ok=True)
            await response.aclose()
            await client.aclose()
            lock.release()

    def _reuse_by_url(
        self,
        source: SourceConfig,
        path: str,
        metadata: bool,
        urls: tuple[tuple[str, str], ...],
    ) -> CacheResult | None:
        for requested_url, _ in urls:
            reusable = self.store.find_by_upstream_url(
                source.pool, requested_url, metadata=metadata
            )
            if not reusable:
                continue
            reused = self.store.upsert(
                source_id=source.name,
                path=path,
                upstream_url=reusable.upstream_url,
                final_url=reusable.final_url,
                sha256=reusable.sha256,
                size=reusable.size,
                content_type=reusable.content_type,
                metadata=metadata,
                etag=reusable.etag,
                last_modified=reusable.last_modified,
            )
            self.store.touch(source.name, path)
            return CacheResult(self.store.file_path(source.name, path), reused, "REUSED")
        return None

    async def _fetch(
        self,
        source: SourceConfig,
        path: str,
        metadata: bool,
        cached: ArtifactRecord | None,
        target: Path,
        urls: tuple[tuple[str, str], ...],
    ) -> CacheResult:
        conditional_headers: dict[str, str] = {}
        if metadata and cached and target.is_file():
            if cached.etag:
                conditional_headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                conditional_headers["If-Modified-Since"] = cached.last_modified

        timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
        failures: list[str] = []
        all_not_found = True
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for requested_url, upstream in urls:
                try:
                    async with client.stream("GET", requested_url, headers=conditional_headers) as response:
                        self.store.record_attempt(
                            source.name, path, upstream, response.status_code, None
                        )
                        if response.status_code == 304 and cached and target.is_file():
                            refreshed = self.store.refresh_not_modified(source.name, path)
                            return CacheResult(target, refreshed, "REVALIDATED")  # type: ignore[arg-type]
                        if response.status_code == 404:
                            if not source.fallback_on_not_found:
                                raise PackageNotFound(path)
                            failures.append(f"{upstream}: HTTP 404")
                            continue
                        all_not_found = False
                        if response.status_code >= 500:
                            failures.append(f"{upstream}: HTTP {response.status_code}")
                            continue
                        if response.status_code != 200:
                            raise UpstreamUnavailable(f"{upstream}: HTTP {response.status_code}")

                        temporary_path = self.store.write_temp(source.name)
                        try:
                            with temporary_path.open("wb") as output:
                                async for chunk in response.aiter_bytes(1024 * 1024):
                                    output.write(chunk)
                            sha256, size = self.store.publish(temporary_path, source.name, path)
                        finally:
                            temporary_path.unlink(missing_ok=True)

                        record = self.store.upsert(
                            source_id=source.name,
                            path=path,
                            upstream_url=upstream,
                            final_url=str(response.url),
                            sha256=sha256,
                            size=size,
                            content_type=response.headers.get("content-type"),
                            metadata=metadata,
                            etag=response.headers.get("etag"),
                            last_modified=response.headers.get("last-modified"),
                        )
                        return CacheResult(self.store.file_path(source.name, path), record, "MISS")
                except PackageNotFound:
                    raise
                except httpx.HTTPError as exc:
                    all_not_found = False
                    self.store.record_attempt(source.name, path, upstream, None, str(exc))
                    failures.append(f"{upstream}: {exc}")

        if all_not_found:
            raise PackageNotFound(path)
        raise UpstreamUnavailable("; ".join(failures) or "No upstream was reachable")
