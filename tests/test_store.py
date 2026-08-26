from pathlib import Path

import pytest

from app.store import CacheStore, UnsafePathError, is_metadata_path, normalise_path


def test_normalise_path_rejects_traversal():
    with pytest.raises(UnsafePathError):
        normalise_path("linux-64/../../secret")


def test_metadata_paths_include_modern_conda_formats():
    assert is_metadata_path("linux-64/repodata.json.zst")
    assert is_metadata_path("linux-64/repodata_shards.msgpack.zst")
    assert is_metadata_path("noarch/current_repodata.json")
    assert not is_metadata_path("linux-64/python-3.12.0-0.conda")


def test_publish_records_hash_and_size(tmp_path: Path):
    store = CacheStore(tmp_path)
    temporary = store.write_temp("conda-forge")
    temporary.write_bytes(b"conda-cache")
    digest, size = store.publish(temporary, "conda-forge", "noarch/example-1.conda")

    assert size == len(b"conda-cache")
    assert digest == "80e17e9e610ab276c88eb21c8633e0f2215a4c3b775430f0eda6db06bf8ad627"
    assert store.file_path("conda-forge", "noarch/example-1.conda").read_bytes() == b"conda-cache"
    store.close()


def test_sources_share_only_their_package_manager_pool(tmp_path: Path):
    store = CacheStore(tmp_path, {"conda-forge": "conda", "nvidia": "conda", "pypi": "pip"})
    conda_first = store.store_bytes(
        source_id="conda-forge", path="linux-64/demo.conda", content=b"same", upstream_url="https://a/demo", metadata=False
    )
    conda_second = store.store_bytes(
        source_id="nvidia", path="linux-64/demo.conda", content=b"same", upstream_url="https://b/demo", metadata=False
    )
    pip = store.store_bytes(
        source_id="pypi", path="files/demo.whl", content=b"same", upstream_url="https://p/demo", metadata=False
    )

    assert conda_first.pool == conda_second.pool == "conda"
    assert store.file_path("conda-forge", "linux-64/demo.conda") == store.file_path("nvidia", "linux-64/demo.conda")
    assert pip.pool == "pip"
    assert store.file_path("pypi", "files/demo.whl") != store.file_path("conda-forge", "linux-64/demo.conda")
    pools = {row["pool"]: row for row in store.statistics()["pools"]}
    assert pools["conda"]["blob_count"] == 1
    assert pools["pip"]["blob_count"] == 1
    store.close()


def test_pool_blob_search_groups_duplicate_routes(tmp_path: Path):
    store = CacheStore(tmp_path)
    store.store_bytes(
        source_id="conda-forge",
        path="linux-64/demo-1.0-0.conda",
        content=b"same-package",
        upstream_url="https://conda.anaconda.org/conda-forge/linux-64/demo-1.0-0.conda",
        metadata=False,
    )
    store.store_bytes(
        source_id="conda-external-example",
        path="linux-64/demo-1.0-0.conda",
        content=b"same-package",
        upstream_url="https://conda.anaconda.org/conda-forge/linux-64/demo-1.0-0.conda",
        metadata=False,
    )

    total, route_total, blobs = store.search_pool_blobs("conda", "demo-1.0-0.conda")

    assert total == 1
    assert route_total == 2
    assert blobs[0]["route_count"] == 2
    assert blobs[0]["source_count"] == 2
    store.close()
