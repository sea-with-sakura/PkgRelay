import asyncio
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, SourceConfig
from app.gateway import Gateway
from app.main import create_app
from app.pypi import PypiGateway
from app.store import CacheStore


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        pass


def test_pypi_index_refreshes_after_ttl_and_honors_explicit_revalidation(tmp_path: Path):
    class MutableIndexHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/simple/demo/":
                self.send_error(404)
                return
            body = self.server.index_body  # type: ignore[attr-defined]
            etag = self.server.index_etag  # type: ignore[attr-defined]
            self.server.requests.append(dict(self.headers))  # type: ignore[attr-defined]
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A003
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), MutableIndexHandler)
    server.index_body = b'<a href="https://files.test/demo-1.0.whl">demo-1.0.whl</a>'  # type: ignore[attr-defined]
    server.index_etag = '"v1"'  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        upstream = f"http://127.0.0.1:{server.server_port}"
        settings = Settings(
            cache_dir=tmp_path / "cache",
            import_roots=(),
            sources={"pypi": SourceConfig("pypi", (upstream,), metadata_ttl_seconds=600, kind="pypi")},
        )
        app = create_app(settings)
        with TestClient(app) as client:
            first = client.get("/get/pypi/pypi/simple/demo/")
            assert first.status_code == 200
            assert "demo-1.0.whl" in first.text
            assert first.headers["x-cache"] == "MISS"

            # TTL expiry causes a conditional upstream request without any
            # client cache directive.  A 304 renews fetched_at, not access time.
            app.state.store.db.execute(
                "UPDATE artifacts SET fetched_at = ? WHERE source_id = 'pypi'",
                (time.time() - 601,),
            )
            app.state.store.db.commit()
            refreshed = client.get("/get/pypi/pypi/simple/demo/")
            assert refreshed.headers["x-cache"] == "REVALIDATED"
            assert server.requests[-1]["If-None-Match"] == '"v1"'  # type: ignore[attr-defined]

            server.index_body = b'<a href="https://files.test/demo-2.0.whl">demo-2.0.whl</a>'  # type: ignore[attr-defined]
            server.index_etag = '"v2"'  # type: ignore[attr-defined]
            # The preceding 304 made the index fresh again, so this request
            # proves no-cache is an optional server-side override, not a TTL
            # workaround required of normal clients.
            forced = client.get("/get/pypi/pypi/simple/demo/", headers={"Cache-Control": "no-cache"})
            assert forced.headers["x-cache"] == "MISS"
            assert "demo-2.0.whl" in forced.text
    finally:
        server.shutdown()
        server.server_close()


def test_pypi_simple_page_rewrites_artifact_to_cached_gateway(tmp_path: Path):
    upstream_root = tmp_path / "upstream"
    (upstream_root / "simple" / "demo").mkdir(parents=True)
    (upstream_root / "packages").mkdir()
    (upstream_root / "packages" / "demo-1.0-py3-none-any.whl").write_bytes(b"wheel-data")
    (upstream_root / "simple" / "demo" / "index.html").write_text(
        '<a href="../../packages/demo-1.0-py3-none-any.whl#sha256=abc">demo</a>'
    )
    handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
        *args, directory=str(upstream_root), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        store = CacheStore(tmp_path / "cache")
        upstream = f"http://127.0.0.1:{server.server_port}"
        pypi = PypiGateway(store, Gateway(store))
        source = SourceConfig(name="pypi", upstreams=(upstream,), kind="pypi")

        index, status = asyncio.run(pypi.simple_index(source, "Demo", "http://cache.test"))
        token = re.search(r"/files/([0-9a-f]{64})/", index)
        artifact = asyncio.run(pypi.artifact(source, token.group(1)))  # type: ignore[union-attr]

        assert status == "MISS"
        assert "http://cache.test/get/pypi/pypi/files/" in index
        assert "#sha256=abc" in index
        assert artifact.file_path.read_bytes() == b"wheel-data"
        assert artifact.record.upstream_url.endswith("/packages/demo-1.0-py3-none-any.whl")
        store.close()
    finally:
        server.shutdown()
        server.server_close()


def test_pypi_source_can_use_a_non_simple_upstream_index_path(tmp_path: Path):
    upstream_root = tmp_path / "upstream"
    (upstream_root / "torch").mkdir(parents=True)
    (upstream_root / "torch" / "index.html").write_text("<a href=\"torch.whl\">torch</a>")
    handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
        *args, directory=str(upstream_root), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        store = CacheStore(tmp_path / "cache")
        source = SourceConfig(
            name="custom-index",
            upstreams=(f"http://127.0.0.1:{server.server_port}",),
            kind="pypi",
            index_path_template="{project}/",
        )

        index, _ = asyncio.run(PypiGateway(store, Gateway(store)).simple_index(source, "torch", "http://cache.test"))

        assert "/get/pypi/custom-index/files/" in index
        store.close()
    finally:
        server.shutdown()
        server.server_close()


def test_default_pypi_prefers_imported_pool_links(tmp_path: Path):
    store = CacheStore(tmp_path / "cache", {"pypi": "pip", "import-pip": "pip"})
    source = SourceConfig(name="pypi", upstreams=("http://127.0.0.1:9",), kind="pypi")
    store.store_bytes(
        source_id="import-pip",
        path="pypi/imported/example/demo-1.0-py3-none-any.whl",
        content=b"wheel-data",
        upstream_url="file:///host-cache/demo-1.0-py3-none-any.whl",
        metadata=False,
    )
    store.register_pypi_link(
        "import-pip",
        "demo",
        "file:///host-cache/demo-1.0-py3-none-any.whl",
        "pypi/imported/example/demo-1.0-py3-none-any.whl",
        "demo-1.0-py3-none-any.whl",
    )

    pypi = PypiGateway(store, Gateway(store), {"pypi": source})
    index, status = asyncio.run(pypi.simple_index(source, "demo", "http://cache.test"))
    token = re.search(r"/get/pypi/import-pip/files/([0-9a-f]{64})/", index)
    imported = SourceConfig("import-pip", ("https://pypi.org",), kind="pypi")
    artifact = asyncio.run(pypi.artifact(imported, token.group(1)))  # type: ignore[union-attr]

    assert status == "HIT"
    assert artifact.file_path.read_bytes() == b"wheel-data"
    store.close()
