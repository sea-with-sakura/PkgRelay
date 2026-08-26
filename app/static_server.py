"""High-throughput LAN delivery for already verified cache Blobs."""

from __future__ import annotations

import argparse
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_POOLS = frozenset({"conda", "pip"})
_COPY_BLOCK_SIZE = 64 * 1024


class BlobHandler(BaseHTTPRequestHandler):
    """Serve only content-addressed blobs; routes remain owned by FastAPI."""

    root = Path("/data/files")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _blob_path(self) -> Path | None:
        parts = urlsplit(self.path).path.split("/")
        if len(parts) != 4 or parts[1] != "blobs" or parts[2] not in _POOLS:
            return None
        sha256 = parts[3]
        if not _SHA256.fullmatch(sha256):
            return None
        return self.root / parts[2] / "blobs" / sha256[:2] / sha256

    def _serve(self, body: bool) -> None:
        blob = self._blob_path()
        if blob is None or not blob.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = blob.stat().st_size
        start, end = 0, size - 1
        requested_range = self.headers.get("Range", "")
        if requested_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested_range.strip())
            if not match:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            left, right = match.groups()
            if left:
                start = int(left)
                end = int(right) if right else end
            elif right:
                start = max(size - int(right), 0)
            else:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if requested_range else HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if requested_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-PkgRelay-Delivery", "static")
        self.end_headers()
        if not body:
            return
        # pip uses a regular, full GET while some clients resume with Range.
        # Keep both code paths on the same bounded write loop.  In particular,
        # do not delegate full responses to ``copyfileobj``: on this server its
        # unbounded stream path can leave a full response stalled even though
        # the equivalent Range request is fast.
        with blob.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(_COPY_BLOCK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
            self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        self._serve(body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(body=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="PkgRelay cached Blob delivery")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=45613)
    parser.add_argument("--root", type=Path, default=Path("/data/files"))
    args = parser.parse_args()
    BlobHandler.root = args.root
    ThreadingHTTPServer((args.host, args.port), BlobHandler).serve_forever()


if __name__ == "__main__":
    main()
