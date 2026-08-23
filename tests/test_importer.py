import asyncio
import json
import tarfile
import zipfile
from pathlib import Path

from app.config import Settings, SourceConfig
from app.importer import import_conda_cache, import_pip_cache
from app.pypi import PypiGateway
from app.gateway import Gateway
from app.store import CacheStore


def make_package(path: Path) -> None:
    info_dir = path.parent / "info"
    info_dir.mkdir()
    index = info_dir / "index.json"
    index.write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0",
                "build": "0",
                "build_number": 0,
                "depends": ["python >=3.11"],
                "subdir": "linux-64",
            }
        )
    )
    with tarfile.open(path, "w:bz2") as archive:
        archive.add(index, arcname="info/index.json")


def test_import_conda_cache_generates_repodata(tmp_path: Path):
    package_cache = tmp_path / "pkgs"
    package_cache.mkdir()
    make_package(package_cache / "demo-1.0-0.tar.bz2")
    settings = Settings(
        cache_dir=tmp_path / "gateway-cache",
        import_roots=(package_cache,),
        sources={"conda-forge": SourceConfig("conda-forge", ("https://conda.anaconda.org/conda-forge",))},
    )

    summary = import_conda_cache(settings, package_cache)

    assert summary.imported == 1
    store = CacheStore(settings.cache_dir)
    assert store.file_path("import-conda", "linux-64/demo-1.0-0.tar.bz2").is_file()
    store.close()


def test_import_pip_cache_makes_an_offline_simple_index(tmp_path: Path):
    package_cache = tmp_path / "pip"
    package_cache.mkdir()
    wheel = package_cache / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("demo-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: Demo\nVersion: 1.0\n")
    settings = Settings(
        cache_dir=tmp_path / "gateway-cache",
        import_roots=(package_cache,),
        sources={"pypi": SourceConfig("pypi", ("https://pypi.org",), kind="pypi")},
    )

    summary = import_pip_cache(settings, package_cache)
    store = CacheStore(settings.cache_dir)
    index, _ = asyncio.run(
        PypiGateway(store, Gateway(store)).simple_index(
            settings.sources["pypi"], "demo", "http://cache.test"
        )
    )

    assert summary.imported == 1
    assert summary.projects == ("demo",)
    assert "/get/pypi/import-pip/files/" in index
    store.close()
