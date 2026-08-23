from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, SourceConfig
from app.main import create_app


def test_dashboard_and_status_are_available(tmp_path: Path):
    settings = Settings(
        cache_dir=tmp_path / "cache",
        import_roots=(tmp_path / "packages",),
        sources={
            "conda-forge": SourceConfig("conda-forge", ("https://conda.anaconda.org/conda-forge",)),
            "pypi": SourceConfig("pypi", ("https://pypi.org",), kind="pypi"),
        },
    )
    app = create_app(settings)
    app.state.store.store_bytes(
        source_id="conda-forge",
        path="linux-64/demo-1.0-0.conda",
        content=b"demo",
        upstream_url="file:///cache/demo-1.0-0.conda",
        metadata=False,
    )
    with TestClient(app) as client:
        dashboard = client.get("/")
        status = client.get("/api/v1/status")
        artifacts = client.get("/api/v1/sources/conda-forge/artifacts?query=demo")
        pool_artifacts = client.get("/api/v1/pools/conda/artifacts?query=demo")
        bootstrap = client.get("/bootstrap/setenv.sh")
        routes = client.get("/bootstrap/client/conda-routes.conf")

    assert dashboard.status_code == 200
    assert "PkgRelay" in dashboard.text
    assert status.status_code == 200
    assert status.json()["import_mode"] == "host-side-only"
    assert status.json()["cache"]["artifact_count"] == 1
    assert artifacts.json()["total"] == 1
    assert pool_artifacts.json()["total"] == 1
    assert {pool["pool"] for pool in status.json()["cache"]["pools"]} == {"conda", "pip"}
    assert bootstrap.status_code == 200
    assert "PkgRelay 缓存客户端" in bootstrap.text
    assert "PKGRELAY_URL" in bootstrap.text
    assert "conda-wrapper.sh" in bootstrap.text
    assert "安装：接入缓存层" in bootstrap.text
    assert "卸载：移除缓存层" in bootstrap.text
    assert "删除旧版 PkgRelay / conda-cache 客户端" in bootstrap.text
    assert "pip config --user set" not in bootstrap.text
    assert "client-update.sh" not in bootstrap.text
    assert routes.status_code == 200
    assert "https://conda.anaconda.org/conda-forge\thttp://testserver/get/conda-forge" in routes.text


def test_static_conda_channel_serves_a_cached_package(tmp_path: Path):
    upstream = "https://conda.example.test/label/stable"
    settings = Settings(
        cache_dir=tmp_path / "cache",
        import_roots=(),
        sources={
            "example": SourceConfig("example", (upstream,)),
            "pypi": SourceConfig("pypi", ("https://pypi.org",), kind="pypi"),
        },
    )
    app = create_app(settings)
    app.state.store.store_bytes(
        source_id="example",
        path="linux-64/demo-1.0-0.conda",
        content=b"demo",
        upstream_url=f"{upstream}/linux-64/demo-1.0-0.conda",
        metadata=False,
    )
    with TestClient(app) as client:
        response = client.get("/get/example/linux-64/demo-1.0-0.conda")

    assert response.status_code == 200
    assert response.content == b"demo"
    assert response.headers["x-cache"] == "HIT"
