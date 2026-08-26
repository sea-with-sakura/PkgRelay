from pathlib import Path
import re

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
        client_conda_channels=("conda-forge",),
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
        routes = client.get("/bootstrap/client/condarc.yaml")
        version = client.get("/bootstrap/client/version")

    assert dashboard.status_code == 200
    assert "PkgRelay" in dashboard.text
    assert status.status_code == 200
    assert status.json()["import_mode"] == "host-side-only"
    assert status.json()["cache"]["artifact_count"] == 1
    assert artifacts.json()["total"] == 1
    assert pool_artifacts.json()["total"] == 1
    assert {pool["pool"] for pool in status.json()["cache"]["pools"]} == {"conda", "pip"}
    assert bootstrap.status_code == 200
    assert "== PkgRelay ==" in bootstrap.text
    assert "PKGRELAY_URL" in bootstrap.text
    assert "Gateway:" in bootstrap.text
    assert "pip config --user set" not in bootstrap.text
    assert "client-update.sh" not in bootstrap.text
    assert routes.status_code == 200
    assert "channels:\n  - conda-forge" in routes.text
    assert "conda-forge: http://testserver/get" in routes.text
    assert version.status_code == 200
    assert re.fullmatch(r"[A-Za-z0-9._-]+", version.text.strip())


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
    assert response.headers["content-length"] == "4"


def test_client_download_usage_is_aggregated_by_machine_and_user(tmp_path: Path):
    settings = Settings(
        cache_dir=tmp_path / "cache",
        import_roots=(),
        sources={"example": SourceConfig("example", ("https://conda.example.test",))},
    )
    app = create_app(settings)
    app.state.store.store_bytes(
        source_id="example",
        path="linux-64/demo-1.0-0.conda",
        content=b"demo",
        upstream_url="https://conda.example.test/linux-64/demo-1.0-0.conda",
        metadata=False,
    )
    client_id = "a" * 32
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/clients/register",
            params={"client_id": client_id, "machine": "gpu-01", "username": "sakura"},
        )
        response = client.get(f"/client/{client_id}/get/example/linux-64/demo-1.0-0.conda")
        usage = client.get("/api/v1/usage")
        routes = client.get(f"/bootstrap/client/condarc.yaml?client_id={client_id}")

    assert registered.status_code == 200
    assert response.status_code == 200
    assert response.content == b"demo"
    assert usage.json()["totals"] == {
        "client_count": 1,
        "downloads": 1,
        "bytes_served": 4,
        "cache_hits": 1,
        "bytes_saved": 4,
        "machine_count": 1,
    }
    assert usage.json()["clients"][0]["machine"] == "gpu-01"
    assert f"/client/{client_id}/get" in routes.text
    assert "always_copy" not in routes.text
