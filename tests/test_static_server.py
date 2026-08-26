import http.client
import threading
from pathlib import Path

from app.static_server import BlobHandler
from http.server import ThreadingHTTPServer


def _request(server: ThreadingHTTPServer, path: str, headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    result = response.status, dict(response.getheaders()), body
    connection.close()
    return result


def test_full_and_range_blob_responses_have_identical_bytes(tmp_path: Path):
    sha256 = "a" * 64
    payload = (b"pkgrelay-static-data-" * 10_000) + b"end"
    blob = tmp_path / "pip" / "blobs" / sha256[:2] / sha256
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)

    original_root = BlobHandler.root
    BlobHandler.root = tmp_path
    server = ThreadingHTTPServer(("127.0.0.1", 0), BlobHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = f"/blobs/pip/{sha256}"
        status, headers, body = _request(server, path)
        assert status == 200
        assert headers["Content-Length"] == str(len(payload))
        assert headers["X-PkgRelay-Delivery"] == "static"
        assert body == payload

        status, headers, body = _request(server, path, {"Range": "bytes=7-53"})
        assert status == 206
        assert headers["Content-Range"] == f"bytes 7-53/{len(payload)}"
        assert body == payload[7:54]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        BlobHandler.root = original_root
