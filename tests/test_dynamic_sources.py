import base64

import pytest

from app.main import DynamicSourceError, _decode_dynamic_upstream
from app.store import CacheStore
from app.sync import ClientSync


def token(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def test_dynamic_source_decodes_and_normalises_an_https_url():
    assert _decode_dynamic_upstream(
        token("https://DOWNLOAD.PYTORCH.ORG/whl/cu132/"),
    ) == "https://download.pytorch.org/whl/cu132"
    assert _decode_dynamic_upstream(
        token("https://example.invalid/simple"),
    ) == "https://example.invalid/simple"


def test_dynamic_source_rejects_http_urls():
    with pytest.raises(DynamicSourceError):
        _decode_dynamic_upstream(token("http://pypi.org/simple"))


def test_dynamic_sources_are_persisted_by_canonical_upstream(tmp_path):
    store = CacheStore(tmp_path / "cache")
    first = store.ensure_dynamic_pypi_source("https://download.pytorch.org/whl/cu132")
    second = store.ensure_dynamic_pypi_source("https://download.pytorch.org/whl/cu132")

    assert first["source_id"] == second["source_id"]
    assert str(first["source_id"]).startswith("external-")
    assert len(list(store.iter_dynamic_pypi_sources())) == 1
    store.close()


def test_identical_artifacts_share_one_content_addressed_blob(tmp_path):
    store = CacheStore(tmp_path / "cache")
    first = store.store_bytes(
        source_id="pypi",
        path="pypi/files/one/demo.whl",
        content=b"same-wheel",
        upstream_url="https://pypi.org/packages/demo.whl",
        metadata=False,
    )
    second = store.store_bytes(
        source_id="external-demo",
        path="pypi/files/two/demo.whl",
        content=b"same-wheel",
        upstream_url="https://example.test/packages/demo.whl",
        metadata=False,
    )

    assert first.sha256 == second.sha256
    assert store.file_path(first.source_id, first.path) == store.file_path(second.source_id, second.path)
    assert store.statistics()["blob_count"] == 1
    assert store.statistics()["total_bytes"] == len(b"same-wheel")
    store.close()


def test_conda_sync_derives_an_unconfigured_channel_from_archive_url():
    source_id, path, upstream, final_url = ClientSync._dynamic_conda_destination(
        "demo-1.0-0.conda",
        "https://packages.example.test/conda/label/stable/linux-64/demo-1.0-0.conda",
    )

    assert source_id.startswith("conda-external-")
    assert path == "linux-64/demo-1.0-0.conda"
    assert upstream == "https://packages.example.test/conda/label/stable"
    assert final_url.endswith("/linux-64/demo-1.0-0.conda")
