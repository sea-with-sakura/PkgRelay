from __future__ import annotations

import base64
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ConfigurationError, Settings, SourceConfig, load_settings
from .gateway import Gateway, PackageNotFound, StreamingCacheResult, UpstreamUnavailable
from .importer import PIP_IMPORT_SOURCE
from .pypi import InvalidProject, PypiGateway
from .store import CacheStore, UnsafePathError, is_metadata_path
from .sync import ClientSync, SyncError


class DynamicSourceError(ValueError):
    pass


def _decode_dynamic_upstream(token: str) -> str:
    if not token or len(token) > 4096:
        raise DynamicSourceError("Invalid dynamic source token")
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        raise DynamicSourceError("Invalid dynamic source token") from None
    if len(raw) > 2048:
        raise DynamicSourceError("Dynamic source URL is too long")
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DynamicSourceError("Dynamic PyPI sources must be HTTPS URLs without credentials")
    if parsed.query or parsed.fragment:
        raise DynamicSourceError("Dynamic PyPI source URL cannot contain query or fragment")
    host = parsed.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise DynamicSourceError("Dynamic PyPI source must use an approved hostname, not an IP address")
    path = parsed.path.rstrip("/")
    if ".." in unquote(path).split("/"):
        raise DynamicSourceError("Dynamic PyPI source path is unsafe")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DynamicSourceError("Dynamic PyPI source port is invalid") from exc
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit(("https", netloc, path, "", ""))




def _minimal_bootstrap_script(cache_url: str) -> str:
    """Render the deliberately small, non-migrating client installer."""
    template = (Path(__file__).parents[1] / "client" / "setenv.sh").read_text()
    return template.replace("__PKGRELAY_URL__", cache_url.rstrip("/"))


def _conda_client_config(settings: Settings, cache_url: str) -> str:
    """Render the user-level Conda settings owned by PkgRelay."""
    base = f"{cache_url.rstrip('/')}/get"
    defaults = [name for name in ("defaults-main", "defaults-r") if name in settings.sources]
    channels = list(settings.client_conda_channels)
    custom = [
        source.name for source in settings.sources.values()
        if source.kind == "conda" and source.name not in {"defaults-main", "defaults-r"}
    ]
    lines = ["channels:"]
    lines.extend(f"  - {name}" for name in channels)
    lines.append(f"channel_priority: {settings.client_conda_channel_priority}")
    lines.append("default_channels:")
    lines.extend(f"  - {base}/{name}" for name in defaults)
    lines.append("custom_channels:")
    lines.extend(f"  {name}: {base}" for name in custom)
    return "\n".join(lines) + "\n"


def _decode_client_origin(token: str) -> str:
    if not token:
        return ""
    if len(token) > 8192:
        raise SyncError("Origin token is too long")
    try:
        padded = token + "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise SyncError("Invalid origin token") from exc




def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = CacheStore(
        settings.cache_dir,
        {**{name: source.pool for name, source in settings.sources.items()}, "import-conda": "conda", PIP_IMPORT_SOURCE: "pip"},
    )
    gateway = Gateway(store)
    pypi_gateway = PypiGateway(store, gateway, settings.sources)
    client_sync = ClientSync(settings, store)

    def dynamic_source(source_id: str) -> SourceConfig | None:
        row = store.get_dynamic_pypi_source(source_id)
        if row is None:
            return None
        return SourceConfig(
            name=str(row["source_id"]),
            upstreams=(str(row["upstream_url"]),),
            metadata_ttl_seconds=0,
            kind="pypi",
            index_path_template="{project}/",
        )


    def pypi_source(source_id: str) -> SourceConfig | None:
        source = settings.sources.get(source_id)
        if source and source.kind == "pypi":
            return source
        if source_id == PIP_IMPORT_SOURCE:
            # Imported files are always resolved from a pre-existing local
            # blob; the placeholder is never contacted for a valid link.
            return SourceConfig(name=PIP_IMPORT_SOURCE, upstreams=("https://pypi.org",), kind="pypi")
        return dynamic_source(source_id)

    async def pypi_index_response(request: Request, source: SourceConfig, project: str) -> HTMLResponse:
        try:
            content, cache_status = await pypi_gateway.simple_index(
                source, project, str(request.base_url).rstrip("/")
            )
        except InvalidProject as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PackageNotFound as exc:
            raise HTTPException(status_code=404, detail=f"Python package not found: {exc}") from exc
        except UpstreamUnavailable as exc:
            raise HTTPException(status_code=502, detail=f"All upstreams failed: {exc}") from exc
        return HTMLResponse(
            content,
            headers={"X-Cache": cache_status, "Cache-Control": f"public, max-age={source.metadata_ttl_seconds}"},
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        store.close()

    app = FastAPI(title="PkgRelay", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.gateway = gateway
    web_root = Path(__file__).parent / "web"
    app.mount("/assets", StaticFiles(directory=web_root), name="assets")

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "sources": sorted(settings.sources)}

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(web_root / "index.html", media_type="text/html")

    @app.get("/bootstrap/setenv.sh", include_in_schema=False)
    async def bootstrap_setenv(request: Request) -> Response:
        return Response(
            _minimal_bootstrap_script(str(request.base_url).rstrip("/")),
            media_type="text/x-shellscript",
            headers={"Content-Disposition": "attachment; filename=setenv.sh", "Cache-Control": "no-store"},
        )

    @app.get("/bootstrap/client/{name}", include_in_schema=False)
    async def bootstrap_client_file(request: Request, name: str) -> Response:
        if name == "condarc.yaml":
            return Response(
                _conda_client_config(settings, str(request.base_url).rstrip("/")),
                media_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )
        allowed = {"pip-wrapper.sh", "cache-sync.sh"}
        if name not in allowed:
            raise HTTPException(status_code=404, detail="Unknown client file")
        return Response(
            (Path(__file__).parents[1] / "client" / name).read_text(),
            media_type="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/status")
    async def status() -> dict[str, object]:
        return {
            "status": "ok",
            "cache_dir": str(settings.cache_dir),
            "import_mode": "host-side-only",
            "cache": store.statistics(),
        }

    @app.get("/api/v1/sources")
    async def list_sources() -> dict[str, object]:
        dynamic_sources = [
            {
                "name": str(row["source_id"]),
                "upstreams": (str(row["upstream_url"]),),
                "metadata_ttl_seconds": 0,
                "kind": "pypi",
                "dynamic": True,
            }
            for row in store.iter_dynamic_pypi_sources()
        ]
        return {
            "sources": [
                {
                    "name": source.name,
                    "upstreams": source.upstreams,
                    "metadata_ttl_seconds": source.metadata_ttl_seconds,
                    "kind": source.kind,
                }
                for source in settings.sources.values()
            ] + dynamic_sources
        }

    def client_sync_enabled() -> None:
        if not settings.client_sync_enabled:
            raise HTTPException(status_code=403, detail="Client cache sync is disabled")

    def client_sync_response(record) -> dict[str, object]:
        return {
            "status": "ready",
            "pool": record.pool,
            "source_id": record.source_id,
            "path": record.path,
            "sha256": record.sha256,
            "size": record.size,
        }

    @app.get("/api/v1/client-sync/{kind}/{sha256}")
    async def client_sync_check(kind: str, sha256: str, filename: str, origin: str = ""):
        client_sync_enabled()
        try:
            record = client_sync.register(kind, sha256, filename, _decode_client_origin(origin))
        except SyncError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="Package blob is not cached")
        return client_sync_response(record)

    @app.put("/api/v1/client-sync/{kind}/{sha256}")
    async def client_sync_upload(request: Request, kind: str, sha256: str, filename: str, origin: str = ""):
        client_sync_enabled()
        try:
            record = await client_sync.upload(
                request, kind, sha256, filename, _decode_client_origin(origin)
            )
        except SyncError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(client_sync_response(record), status_code=201)

    @app.get("/api/v1/artifacts")
    async def recent_artifacts(limit: int = 100) -> dict[str, object]:
        limit = min(max(limit, 1), 1000)
        return {"artifacts": [record.__dict__ for record in store.iter_recent(limit)]}

    @app.get("/api/v1/sources/{source_id}/artifacts")
    async def source_artifacts(
        source_id: str, query: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, object]:
        if source_id not in settings.sources and dynamic_source(source_id) is None:
            raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        query = query.strip()[:200]
        total, records = store.search_artifacts(source_id, query, limit, offset)
        return {"total": total, "artifacts": [record.__dict__ for record in records]}

    @app.get("/api/v1/pools/{pool}/artifacts")
    async def pool_artifacts(
        pool: str, query: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, object]:
        if pool not in CacheStore.VALID_POOLS:
            raise HTTPException(status_code=404, detail=f"Unknown cache pool: {pool}")
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        total, route_total, blobs = store.search_pool_blobs(pool, query.strip()[:200], limit, offset)
        return {"total": total, "route_total": route_total, "artifacts": blobs}

    # PEP 503 clients (including pip) request project pages with a trailing
    # slash.  Keep the slashless form too for browsers and older clients.
    @app.get("/get/pypi/{source_id}/simple/{project}/")
    @app.get("/get/pypi/{source_id}/simple/{project}")
    async def get_pypi_simple_index(request: Request, source_id: str, project: str):
        source = pypi_source(source_id)
        if not source:
            raise HTTPException(status_code=404, detail=f"Unknown PyPI source: {source_id}")
        return await pypi_index_response(request, source, project)

    @app.get("/get/pypi/external/{upstream_token}/simple/{project}/")
    @app.get("/get/pypi/external/{upstream_token}/simple/{project}")
    async def get_external_pypi_simple_index(
        request: Request, upstream_token: str, project: str
    ):
        try:
            upstream_url = _decode_dynamic_upstream(
                upstream_token
            )
        except DynamicSourceError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        row = store.ensure_dynamic_pypi_source(upstream_url)
        source = dynamic_source(str(row["source_id"]))
        assert source is not None
        return await pypi_index_response(request, source, project)

    @app.head("/get/pypi/{source_id}/files/{token}/{filename}")
    @app.get("/get/pypi/{source_id}/files/{token}/{filename}")
    async def get_pypi_artifact(
        request: Request, source_id: str, token: str, filename: str
    ):
        source = pypi_source(source_id)
        if not source:
            raise HTTPException(status_code=404, detail=f"Unknown PyPI source: {source_id}")
        try:
            # A HEAD response has no body to stream, so retain the existing
            # file-backed path. GET requests stream cache misses immediately.
            result = (
                await pypi_gateway.artifact(source, token)
                if request.method == "HEAD"
                else await pypi_gateway.artifact_streaming(source, token)
            )
        except PackageNotFound as exc:
            raise HTTPException(status_code=404, detail=f"Python package not found: {exc}") from exc
        except UpstreamUnavailable as exc:
            raise HTTPException(status_code=502, detail=f"Upstream failed: {exc}") from exc
        if isinstance(result, StreamingCacheResult):
            headers = {
                "X-Cache": result.cache_status,
                "X-Cache-Source": result.source_id,
                "Cache-Control": "public, max-age=31536000, immutable",
            }
            if result.content_length is not None:
                headers["Content-Length"] = str(result.content_length)
            return StreamingResponse(
                result.body,
                media_type=result.content_type or "application/octet-stream",
                headers=headers,
            )
        return FileResponse(
            result.file_path,
            media_type=result.record.content_type or "application/octet-stream",
            headers={
                "X-Cache": result.cache_status,
                "X-Cache-Source": result.record.source_id,
                "X-Cache-SHA256": result.record.sha256,
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    @app.head("/get/{source_id}/{path:path}")
    @app.get("/get/{source_id}/{path:path}")
    async def get_cached_channel_file(request: Request, source_id: str, path: str):
        source = settings.sources.get(source_id)
        if not source:
            raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
        if source.kind != "conda":
            raise HTTPException(status_code=404, detail="Use the PyPI endpoint for this source")
        try:
            result = (
                await gateway.get(source, path)
                if request.method == "HEAD" or is_metadata_path(path)
                else await gateway.get_streaming(source, path)
            )
        except UnsafePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PackageNotFound as exc:
            raise HTTPException(status_code=404, detail=f"Package not found upstream: {exc}") from exc
        except UpstreamUnavailable as exc:
            raise HTTPException(status_code=502, detail=f"All upstreams failed: {exc}") from exc

        if isinstance(result, StreamingCacheResult):
            headers = {
                "X-Cache": result.cache_status,
                "X-Cache-Source": result.source_id,
                "Cache-Control": "public, max-age=31536000, immutable",
            }
            if result.content_length is not None:
                headers["Content-Length"] = str(result.content_length)
            return StreamingResponse(
                result.body,
                media_type=result.content_type or "application/octet-stream",
                headers=headers,
            )

        cache_control = "public, max-age=31536000, immutable"
        if result.record.is_metadata:
            cache_control = f"public, max-age={source.metadata_ttl_seconds}"
        headers = {
            "X-Cache": result.cache_status,
            "X-Cache-Source": result.record.source_id,
            "X-Cache-SHA256": result.record.sha256,
            "Cache-Control": cache_control,
        }
        return FileResponse(
            result.file_path,
            media_type=result.record.content_type or "application/octet-stream",
            headers=headers,
        )

    return app


try:
    app = create_app()
except ConfigurationError:
    # Importing the module for tooling should not hide the actionable config error.
    # Uvicorn imports this symbol after the operator has supplied config.yaml.
    app = None
