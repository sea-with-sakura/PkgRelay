import asyncio
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import SourceConfig
from app.gateway import Gateway
from app.pypi import PypiGateway
from app.store import CacheStore


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        pass


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
