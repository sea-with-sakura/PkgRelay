from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


class ConfigurationError(ValueError):
    """Raised when the gateway configuration cannot be used safely."""


@dataclass(frozen=True)
class SourceConfig:
    name: str
    upstreams: tuple[str, ...]
    metadata_ttl_seconds: int = 600
    fallback_on_not_found: bool = False
    kind: str = "conda"
    index_path_template: str = "simple/{project}/"

    @property
    def pool(self) -> str:
        return "pip" if self.kind == "pypi" else "conda"


@dataclass(frozen=True)
class Settings:
    cache_dir: Path
    sources: dict[str, SourceConfig]
    import_roots: tuple[Path, ...]
    client_sync_enabled: bool = False
    client_sync_max_upload_bytes: int = 20 * 1024 * 1024 * 1024

    @property
    def database_path(self) -> Path:
        return self.cache_dir / "metadata.sqlite3"


def _validate_upstream(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("Each upstream must be a URL string")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"Invalid upstream URL: {value!r}")
    return value.rstrip("/")


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.getenv("PKGRELAY_CONFIG", "config.yaml"))
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration file not found: {config_path}") from exc

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, dict) or not raw_sources:
        raise ConfigurationError("Configuration must contain at least one source")

    # Deployment can select a persistent volume without changing a shared config file.
    cache_value = os.getenv("PKGRELAY_CACHE_DIR", raw.get("cache_dir", "./data"))
    if not isinstance(cache_value, str):
        raise ConfigurationError("cache_dir must be a string path")
    cache_dir = Path(cache_value)
    if not cache_dir.is_absolute():
        cache_dir = (config_path.parent / cache_dir).resolve()

    configured_import_roots = os.getenv("PKGRELAY_IMPORT_ROOTS")
    raw_import_roots = (
        configured_import_roots.split(os.pathsep)
        if configured_import_roots is not None
        else raw.get("import_roots", [])
    )
    if not isinstance(raw_import_roots, list) or not all(
        isinstance(item, str) for item in raw_import_roots
    ):
        raise ConfigurationError("import_roots must be a list of paths")
    import_roots = tuple(Path(item).resolve() for item in raw_import_roots)

    client_sync = raw.get("client_sync", {})
    if not isinstance(client_sync, dict):
        raise ConfigurationError("client_sync must be a mapping")
    client_sync_enabled = client_sync.get("enabled", False)
    client_sync_max_upload_bytes = client_sync.get("max_upload_bytes", 20 * 1024 * 1024 * 1024)
    if not isinstance(client_sync_enabled, bool):
        raise ConfigurationError("client_sync.enabled must be true or false")
    if not isinstance(client_sync_max_upload_bytes, int) or client_sync_max_upload_bytes <= 0:
        raise ConfigurationError("client_sync.max_upload_bytes must be a positive integer")

    sources: dict[str, SourceConfig] = {}
    for name, source in raw_sources.items():
        if not isinstance(name, str) or not name or "/" in name or ".." in name:
            raise ConfigurationError(f"Invalid source name: {name!r}")
        if not isinstance(source, dict):
            raise ConfigurationError(f"Source {name!r} must be a mapping")
        upstreams = source.get("upstreams")
        kind = source.get("kind", "conda")
        if kind not in {"conda", "pypi"}:
            raise ConfigurationError(f"Source {name!r} kind must be conda or pypi")
        index_path_template = source.get("index_path_template", "simple/{project}/")
        if not isinstance(index_path_template, str) or "{project}" not in index_path_template:
            raise ConfigurationError(
                f"Source {name!r} index_path_template must contain {{project}}"
            )
        if index_path_template.startswith(("/", "http://", "https://")) or ".." in index_path_template:
            raise ConfigurationError(f"Source {name!r} has an unsafe index_path_template")
        if not isinstance(upstreams, list) or not upstreams:
            raise ConfigurationError(f"Source {name!r} needs a non-empty upstreams list")
        ttl = source.get("metadata_ttl_seconds", 600)
        if not isinstance(ttl, int) or ttl < 0:
            raise ConfigurationError(f"Source {name!r} has an invalid metadata TTL")
        fallback_on_not_found = source.get("fallback_on_not_found", False)
        if not isinstance(fallback_on_not_found, bool):
            raise ConfigurationError(
                f"Source {name!r} fallback_on_not_found must be true or false"
            )
        sources[name] = SourceConfig(
            name=name,
            upstreams=tuple(_validate_upstream(item) for item in upstreams),
            metadata_ttl_seconds=ttl,
            fallback_on_not_found=fallback_on_not_found,
            kind=kind,
            index_path_template=index_path_template,
        )

    return Settings(
        cache_dir=cache_dir,
        sources=sources,
        import_roots=import_roots,
        client_sync_enabled=client_sync_enabled,
        client_sync_max_upload_bytes=client_sync_max_upload_bytes,
    )
