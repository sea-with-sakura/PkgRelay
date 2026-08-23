from pathlib import Path

from app.config import load_settings


def test_cache_directory_can_be_overridden_for_container_volume(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("sources:\n  conda-forge:\n    upstreams: [https://conda.anaconda.org/conda-forge]\n")
    monkeypatch.setenv("PKGRELAY_CACHE_DIR", "/data")

    settings = load_settings(config)

    assert settings.cache_dir == Path("/data")
