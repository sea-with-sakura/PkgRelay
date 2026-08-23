import base64
import hashlib
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

    assert dashboard.status_code == 200
    assert "PkgRelay" in dashboard.text
    assert status.status_code == 200
    assert status.json()["import_mode"] == "host-side-only"
    assert status.json()["cache"]["artifact_count"] == 1
    assert artifacts.json()["total"] == 1
    assert pool_artifacts.json()["total"] == 1
    assert {pool["pool"] for pool in status.json()["cache"]["pools"]} == {"conda", "pip"}
    assert bootstrap.status_code == 200
    assert "pip3()" in bootstrap.text
    assert "PKGRELAY_URL" in bootstrap.text
    assert "conda-wrapper.sh" in bootstrap.text
    assert "--override-channels" in bootstrap.text
    assert 'cat > "$client_dir/conda-channels.conf"' not in bootstrap.text
    assert "Conda did not report an effective channel" in bootstrap.text
    assert "append channels nodefaults" not in bootstrap.text
    assert "安装：接入缓存层" in bootstrap.text
    assert "卸载：移除缓存层" in bootstrap.text
    assert "PIP_INDEX_VALUE" in bootstrap.text
    assert "卸载当前用户的缓存配置" in bootstrap.text
    assert "uninstall_cache_client" in bootstrap.text


def test_dynamic_conda_channel_serves_a_cached_package(tmp_path: Path):
    upstream = "https://conda.example.test/label/stable"
    source_id = "conda-external-" + hashlib.sha256(upstream.encode()).hexdigest()[:24]
    settings = Settings(
        cache_dir=tmp_path / "cache",
        import_roots=(),
        sources={"pypi": SourceConfig("pypi", ("https://pypi.org",), kind="pypi")},
    )
    app = create_app(settings)
    app.state.store.store_bytes(
        source_id=source_id,
        path="linux-64/demo-1.0-0.conda",
        content=b"demo",
        upstream_url=f"{upstream}/linux-64/demo-1.0-0.conda",
        metadata=False,
    )
    token = base64.urlsafe_b64encode(upstream.encode()).decode().rstrip("=")
    with TestClient(app) as client:
        response = client.get(f"/get/conda/external/{token}/linux-64/demo-1.0-0.conda")

    assert response.status_code == 200
    assert response.content == b"demo"
    assert response.headers["x-cache"] == "HIT"
