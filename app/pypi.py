from __future__ import annotations

import hashlib
import html
import re
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .config import SourceConfig
from .gateway import CacheResult, Gateway, PackageNotFound, StreamingCacheResult, UpstreamUnavailable
from .store import CacheStore


class InvalidProject(ValueError):
    pass


def normalise_project(project: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", project):
        raise InvalidProject("Invalid Python package name")
    return re.sub(r"[-_.]+", "-", project).lower()


def _url_without_fragment(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _filename(url: str) -> str:
    name = unquote(PurePosixPath(urlsplit(url).path).name)
    if not name or name in {".", ".."}:
        return "download"
    return name.replace("/", "_").replace("\\", "_")


@dataclass(frozen=True)
class SimpleLink:
    url: str
    filename: str
    requires_python: str | None
    yanked: str | None
    source_id: str | None = None
    cache_path: str | None = None


class _SimplePageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[SimpleLink] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = dict(attrs)
        href = values.get("href")
        if not href:
            return
        resolved = urljoin(self.base_url, href)
        parsed = urlsplit(resolved)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return
        self.links.append(
            SimpleLink(
                url=resolved,
                filename=_filename(resolved),
                requires_python=values.get("data-requires-python"),
                yanked=values.get("data-yanked"),
            )
        )


class PypiGateway:
    """PEP 503 HTML proxy backed by the shared artifact store."""

    def __init__(self, store: CacheStore, gateway: Gateway, sources: dict[str, SourceConfig] | None = None):
        self.store = store
        self.gateway = gateway
        self.sources = sources or {}
        self._rendered_indexes: OrderedDict[tuple[str, str, str, str], str] = OrderedDict()
        self._max_rendered_indexes = 8

    @staticmethod
    def _artifact_path(upstream_url: str, filename: str) -> str:
        digest = hashlib.sha256(_url_without_fragment(upstream_url).encode()).hexdigest()
        return f"pypi/files/{digest}/{filename}"

    def _render(self, source: SourceConfig, project: str, links: list[SimpleLink], base_url: str) -> str:
        resolved_links: list[tuple[SimpleLink, str, str, str]] = []
        registrations: list[tuple[str, str, str, str, str]] = []
        for link in links:
            upstream_url = _url_without_fragment(link.url)
            link_source_id = link.source_id or source.name
            cache_path = link.cache_path or self._artifact_path(upstream_url, link.filename)
            token = self.store.pypi_link_token(link_source_id, upstream_url)
            resolved_links.append((link, link_source_id, token, upstream_url))
            registrations.append((link_source_id, project, upstream_url, cache_path, link.filename))
        self.store.register_pypi_links(registrations)

        rendered: list[str] = []
        for link, link_source_id, token, upstream_url in resolved_links:
            fragment = urlsplit(link.url).fragment
            href = f"{base_url}/get/pypi/{link_source_id}/files/{token}/{link.filename}"
            if fragment:
                href += f"#{fragment}"
            attributes = [f'href="{html.escape(href, quote=True)}"']
            if link.requires_python is not None:
                attributes.append(
                    f'data-requires-python="{html.escape(link.requires_python, quote=True)}"'
                )
            if link.yanked is not None:
                attributes.append(f'data-yanked="{html.escape(link.yanked, quote=True)}"')
            rendered.append(
                f"<a {' '.join(attributes)}>{html.escape(link.filename)}</a>"
            )
        title = html.escape(project)
        return f"<!doctype html><html><head><title>Links for {title}</title></head><body>" + "\n".join(rendered) + "</body></html>"

    def _imported_links(self, project: str) -> list[SimpleLink]:
        links: list[SimpleLink] = []
        filenames: set[str] = set()
        for row in self.store.iter_imported_pypi_links(project):
            filename = str(row["filename"])
            if filename in filenames:
                continue
            filenames.add(filename)
            links.append(
                SimpleLink(
                    url=str(row["upstream_url"]),
                    filename=filename,
                    requires_python=None,
                    yanked=None,
                    source_id="import-pip",
                    cache_path=str(row["cache_path"]),
                )
            )
        return links

    async def simple_index(self, source: SourceConfig, project: str, base_url: str) -> tuple[str, str]:
        project = normalise_project(project)
        imported_links = self._imported_links(project)
        try:
            result = await self.gateway.get(
                source,
                f"pypi-index/{project}.html",
                metadata=True,
                remote_path=source.index_path_template.format(project=project),
            )
        except (PackageNotFound, UpstreamUnavailable):
            if imported_links:
                return self._render(source, project, imported_links, base_url), "HIT"
            raise
        parser = _SimplePageParser(result.record.final_url)
        parser.feed(result.file_path.read_text(errors="replace"))
        # Imported files are served first and shadow an upstream file with the
        # same filename; other upstream versions remain available to pip.
        imported_filenames = {link.filename for link in imported_links}
        links = imported_links + [link for link in parser.links if link.filename not in imported_filenames]
        cache_key = (source.name, project, base_url, result.record.sha256)
        if not imported_links:
            cached_html = self._rendered_indexes.get(cache_key)
            if cached_html is not None:
                self._rendered_indexes.move_to_end(cache_key)
                return cached_html, result.cache_status
        rendered = self._render(source, project, links, base_url)
        if not imported_links:
            self._rendered_indexes[cache_key] = rendered
            self._rendered_indexes.move_to_end(cache_key)
            while len(self._rendered_indexes) > self._max_rendered_indexes:
                self._rendered_indexes.popitem(last=False)
        return rendered, result.cache_status

    async def artifact(self, source: SourceConfig, token: str) -> CacheResult:
        link = self.store.get_pypi_link(source.name, token)
        if not link:
            raise PackageNotFound("Unknown or expired package link")
        return await self.gateway.get_url(
            source,
            str(link["cache_path"]),
            str(link["upstream_url"]),
            metadata=False,
        )

    async def artifact_streaming(
        self, source: SourceConfig, token: str
    ) -> CacheResult | StreamingCacheResult:
        link = self.store.get_pypi_link(source.name, token)
        if not link:
            raise PackageNotFound("Unknown or expired package link")
        return await self.gateway.get_url_streaming(
            source,
            str(link["cache_path"]),
            str(link["upstream_url"]),
        )
