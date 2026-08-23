import asyncio
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import SourceConfig
from app.gateway import Gateway, StreamingCacheResult
from app.store import CacheStore


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        pass


def test_gateway_fetches_then_serves_artifact_from_local_cache(tmp_path: Path):
    upstream_root = tmp_path / "upstream"
    (upstream_root / "linux-64").mkdir(parents=True)
    artifact = upstream_root / "linux-64" / "demo-1.0-0.conda"
    artifact.write_bytes(b"cached-conda-artifact")

    handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
        *args, directory=str(upstream_root), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        upstream = f"http://127.0.0.1:{server.server_port}"
        store = CacheStore(tmp_path / "cache")
        gateway = Gateway(store)
        source = SourceConfig(name="test", upstreams=(upstream,))

        first = asyncio.run(gateway.get(source, "linux-64/demo-1.0-0.conda"))
        second = asyncio.run(gateway.get(source, "linux-64/demo-1.0-0.conda"))

        assert first.cache_status == "MISS"
        assert second.cache_status == "HIT"
        assert second.file_path.read_bytes() == b"cached-conda-artifact"
        record = store.get("test", "linux-64/demo-1.0-0.conda")
        assert record is not None
        assert record.upstream_url == upstream
        assert record.hit_count == 1
        store.close()
    finally:
        server.shutdown()
        server.server_close()


def test_conda_reuses_imported_archive_by_exact_upstream_url(tmp_path: Path):
    store = CacheStore(tmp_path / "cache")
    store.store_bytes(
        source_id="import-conda",
        path="linux-64/demo-1.0-0.conda",
        content=b"host-imported",
        upstream_url="https://conda.anaconda.org/conda-forge/linux-64/demo-1.0-0.conda",
        metadata=False,
    )
    gateway = Gateway(store)
    source = SourceConfig("conda-forge", ("https://conda.anaconda.org/conda-forge",))

    result = asyncio.run(gateway.get(source, "linux-64/demo-1.0-0.conda"))

    assert result.cache_status == "REUSED"
    assert result.file_path.read_bytes() == b"host-imported"
    assert store.get("conda-forge", "linux-64/demo-1.0-0.conda") is not None
    store.close()


def test_gateway_streams_pypi_artifact_and_publishes_it_afterward(tmp_path: Path):
    upstream_root = tmp_path / "upstream"
    (upstream_root / "packages").mkdir(parents=True)
    artifact = upstream_root / "packages" / "demo-1.0-py3-none-any.whl"
    artifact.write_bytes(b"wheel-data" * 1024)

    handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
        *args, directory=str(upstream_root), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        store = CacheStore(tmp_path / "cache")
        upstream = f"http://127.0.0.1:{server.server_port}"
        gateway = Gateway(store)
        source = SourceConfig(name="pypi", upstreams=(upstream,), kind="pypi")
        path = "pypi/files/token/demo-1.0-py3-none-any.whl"
        url = f"{upstream}/packages/{artifact.name}"

        async def stream_once():
            result = await gateway.get_url_streaming(source, path, url)
            assert isinstance(result, StreamingCacheResult)
            return b"".join([chunk async for chunk in result.body])

        assert asyncio.run(stream_once()) == artifact.read_bytes()
        cached = store.get("pypi", path)
        assert cached is not None
        assert cached.size == artifact.stat().st_size

        hit = asyncio.run(gateway.get_url_streaming(source, path, url))
        assert hit.cache_status == "HIT"
        assert hit.file_path.read_bytes() == artifact.read_bytes()
        store.close()
    finally:
        server.shutdown()
        server.server_close()
