"""Page discovery: work out which pages exist on the target site.

Order of preference:
  1. explicit list            --pages /,/about,/posts/hello
  2. sitemap.xml              (and sitemap index files)
  3. crawl of homepage links  (breadth-first, capped)

Results are deduplicated by path, normalised (no fragments, no query strings)
and given weights so the homepage gets more traffic than a buried page.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx

from .config import Settings

# Links to these are never treated as pages.
_ASSET_EXTENSIONS = {
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".webm", ".ogg", ".wav", ".pdf", ".zip", ".gz", ".tar",
    ".json", ".xml", ".rss", ".atom", ".txt", ".csv", ".doc", ".docx",
}

_SCHEME_HEADERS = {
    "User-Agent": "trafficlab/1.0 (controlled research traffic; own-site testing)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_MAX_CRAWL_FETCHES = 15     # page fetches during link discovery
_MAX_SITEMAP_CHILDREN = 3   # nested sitemap files to follow


@dataclass(frozen=True)
class PageTarget:
    path: str      # e.g. "/" or "/about"
    url: str       # absolute
    weight: float  # relative share of traffic


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def normalize_url(href: str, origin: str) -> tuple[str, str] | None:
    """Return (path, absolute_url) if href is an internal http(s) page, else None."""
    absolute = urljoin(origin + "/", href.strip())
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return None
    if parts.netloc != urlsplit(origin).netloc:
        return None
    path = parts.path or "/"
    if not path.startswith("/"):
        return None
    if "." in path.rsplit("/", 1)[-1]:          # has a file extension -> asset
        ext = "." + path.rsplit(".", 1)[-1].lower()
        if ext in _ASSET_EXTENSIONS:
            return None
    if len(path) > 1 and path.endswith("/"):    # /about/ and /about are the same page
        path = path.rstrip("/")
    clean = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return path, clean


def _parse_sitemap_locs(text: str) -> list[str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    return [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]


def _is_sitemap_index(text: str) -> bool:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return False
    return root.tag.endswith("sitemapindex")


async def _targets_from_sitemap(client: httpx.AsyncClient, settings: Settings) -> dict[str, PageTarget]:
    origin = settings.url.rstrip("/")
    prefix = urlsplit(settings.url).path.rstrip("/")
    candidates = [
        urljoin(origin + "/", "sitemap.xml"),
        urljoin(origin + "/", "sitemap_index.xml"),
        urljoin(origin + "/", "sitemap-index.xml"),
    ]
    if prefix:
        candidates.insert(0, f"{origin}{prefix}/sitemap.xml")

    for sm_url in candidates:
        try:
            resp = await client.get(sm_url)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200 or "xml" not in resp.headers.get("content-type", "") and "<" not in resp.text[:64]:
            continue
        locs = _parse_sitemap_locs(resp.text)
        if not locs:
            continue
        if _is_sitemap_index(resp.text):
            nested: list[str] = []
            for child in locs[:_MAX_SITEMAP_CHILDREN]:
                try:
                    r = await client.get(child)
                except httpx.HTTPError:
                    continue
                if r.status_code == 200:
                    nested.extend(_parse_sitemap_locs(r.text))
            locs = nested
        found: dict[str, PageTarget] = {}
        for loc in locs:
            norm = normalize_url(loc, origin)
            if norm and norm[1] not in {t.url for t in found.values()}:
                path, url = norm
                found[path] = PageTarget(path=path, url=url, weight=1.0)
        if found:
            return found
    return {}


async def _fetch_links(client: httpx.AsyncClient, url: str) -> list[str]:
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return []
    if resp.status_code >= 400:
        return []
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and not resp.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return []
    parser = _LinkParser()
    try:
        parser.feed(resp.text)
    except Exception:
        return []
    return parser.hrefs


async def _targets_from_crawl(client: httpx.AsyncClient, settings: Settings) -> dict[str, PageTarget]:
    origin = settings.url.rstrip("/")
    found: dict[str, PageTarget] = {}
    frontier: list[str] = [settings.url]
    visited: set[str] = set()
    fetches = 0

    while frontier and fetches < _MAX_CRAWL_FETCHES and len(found) < settings.max_pages:
        current = frontier.pop(0)
        if current in visited:
            continue
        visited.add(current)
        fetches += 1
        for href in await _fetch_links(client, current):
            norm = normalize_url(href, origin)
            if not norm:
                continue
            path, url = norm
            if path in found:
                continue
            found[path] = PageTarget(path=path, url=url, weight=1.0)
            frontier.append(url)
    return found


async def discover(settings: Settings) -> list[PageTarget]:
    """Return the weighted list of pages to hit. Never returns an empty list."""
    if settings.pages:
        origin = settings.url.rstrip("/")
        targets = []
        for raw in settings.pages:
            path = "/" + raw.lstrip("/") if raw != "/" else "/"
            norm = normalize_url(path, origin)
            if norm:
                targets.append(PageTarget(path=norm[0], url=norm[1], weight=1.0))
        return _finalize(targets, settings)

    async with httpx.AsyncClient(
        headers=_SCHEME_HEADERS,
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        found = await _targets_from_sitemap(client, settings)
        if len(found) < settings.max_pages:
            crawled = await _targets_from_crawl(client, settings)
            for path, target in crawled.items():
                found.setdefault(path, target)

    if not found:
        raise RuntimeError("no pages discovered (no sitemap, no links found)")
    return _finalize(list(found.values()), settings)


def _finalize(targets: list[PageTarget], settings: Settings) -> list[PageTarget]:
    """Give the homepage its weight, dedupe, sort, cap."""
    seen: dict[str, PageTarget] = {}
    for t in targets:
        weight = settings.home_weight if t.path == "/" else 1.0
        seen.setdefault(t.path, PageTarget(path=t.path, url=t.url, weight=weight))
    ordered = sorted(seen.values(), key=lambda t: (t.path != "/", t.path))
    return ordered[: settings.max_pages]
