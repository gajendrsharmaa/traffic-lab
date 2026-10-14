"""Local mirror: snapshot your own site and serve it from loopback.

Purpose: give the lab a target that is yours end-to-end - your pages, your
detector code, your demo ad logic - with no CDN, no ad network and no third
party in the path. `--stealth` only works against loopback hosts, so this is
the environment it exists for.

Safety properties:

* tags pointing at ad networks / trackers are STRIPPED from the snapshot
  (AdSense, DoubleClick, Adsterra, ...), so bots browsing the mirror can
  never generate an impression, click or event at a real network;
* the server binds 127.0.0.1 only - nothing outside your machine can reach it;
* same-origin URLs are rewritten to local relative paths, so nothing in the
  pages silently "phones home" to production.

Usage:

    python -m trafficlab.mirror snapshot https://your-site.dev --out mirror/site
    python -m trafficlab.mirror serve --site mirror/site --port 8787
    python run.py --url http://127.0.0.1:8787 --stealth

Caveats worth knowing:
* the strip is static (HTML level). If your ad code is self-hosted JS that
  fetches a network at runtime, keep the real ad snippet out of the local
  test pages - that is the point of the mirror.
* third parties NOT on the blocklist (fonts, CDNs) still load normally.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .config import Settings
from .discovery import discover

# Ad networks / measurement trackers: tags referencing these never survive
# into the snapshot. Extend it freely for your own experiments.
AD_HOST_MARKERS = (
    "googlesyndication.com", "googleadservices.com", "doubleclick.net",
    "google-analytics.com", "googletagmanager.com", "adsterra.com",
    "propellerads.com", "popads.net", "popcash.net", "taboola.com",
    "outbrain.com", "criteo.com", "amazon-adsystem.com", "pubmatic.com",
    "rubiconproject.com", "openx.net", "casalemedia.com", "adnxs.com",
    "scorecardresearch.com", "moatads.com", "zedo.com", "teads.tv",
    "mgid.com", "revcontent.com", "exoclick.com", "juicyads.com",
    "trafficjunky.com", "adskeeper.com",
)

_SCHEME_HEADERS = {
    "User-Agent": "trafficlab-mirror/1.0 (snapshotting own site for local testing)",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Elements that can carry an external resource; matched whole so a removed
# tag takes its closing tag (and inline loader body) with it.
_ELEMENT_RE = re.compile(
    r"<(?P<tag>script|iframe|img|ins|link|object|embed)\b[^>]*?(?:/>|>.*?</(?P=tag)>|>)",
    re.IGNORECASE | re.DOTALL,
)
_URL_ATTR_RE = re.compile(r"(?:src|href|poster)=[\"'](/[^\"'# ]*)[\"']", re.IGNORECASE)
_SRCSET_RE = re.compile(r"srcset=[\"']([^\"']+)[\"']", re.IGNORECASE)

_MAX_ASSET_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------
def strip_third_party(html: str, blocked: tuple[str, ...]) -> tuple[str, int]:
    """Remove every script/iframe/img/link pointing at a blocked host."""
    removed = 0

    def repl(match: re.Match) -> str:
        nonlocal removed
        blob = match.group(0)
        if any(marker in blob for marker in blocked):
            removed += 1
            return ""
        return blob

    return _ELEMENT_RE.sub(repl, html), removed


def local_asset_refs(html: str) -> list[str]:
    """Same-origin resource paths referenced by this page."""
    refs = set(_URL_ATTR_RE.findall(html))
    for value in _SRCSET_RE.findall(html):
        for part in value.split(","):
            url = part.strip().split(" ")[0]
            if url.startswith("/"):
                refs.add(url.split("?")[0])
    return sorted(refs)


def _page_dest(site: Path, path: str) -> Path:
    if path == "/":
        return site / "index.html"
    return site / path.strip("/") / "index.html"


async def snapshot(origin: str, out: Path, max_pages: int, max_assets: int,
                   allow_hosts: list[str], timeout: float) -> dict:
    origin = origin.rstrip("/")
    urlsplit(origin)  # fail loudly on garbage
    blocked = tuple(
        m for m in AD_HOST_MARKERS
        if not any(allow in m for allow in allow_hosts)
    )
    site = out
    site.mkdir(parents=True, exist_ok=True)

    targets = await discover(Settings(url=origin, max_pages=max_pages))
    manifest: dict = {
        "origin": origin, "pages": [], "assets": [],
        "ad_tags_removed": 0, "blocked_markers": list(blocked),
        "snapshotted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    total_removed = 0
    assets_done: set[str] = set()

    async with httpx.AsyncClient(headers=_SCHEME_HEADERS, timeout=timeout,
                                 follow_redirects=True) as client:
        for target in targets:
            try:
                resp = await client.get(target.url)
            except httpx.HTTPError:
                continue
            ctype = resp.headers.get("content-type", "").lower()
            if resp.status_code >= 400 or ("html" not in ctype
                                           and not resp.text.lstrip()[:15].lower().startswith("<!doctype")):
                continue
            html, removed = strip_third_party(resp.text, blocked)
            total_removed += removed
            html = html.replace(origin, "")       # everything same-origin -> local relative
            dest = _page_dest(site, target.path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(html, encoding="utf-8")
            manifest["pages"].append(target.path)
            print(f"  page   {target.path:<24} ({len(html) // 1024} KB, "
                  f"{removed} ad tags stripped)", flush=True)

            for ref in local_asset_refs(html):
                if ref in assets_done or len(assets_done) >= max_assets:
                    continue
                assets_done.add(ref)
                await _fetch_asset(client, origin, ref, site, manifest)
                await asyncio.sleep(0.03)              # be polite to yourself

    manifest["ad_tags_removed"] = total_removed
    (site / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


async def _fetch_asset(client: httpx.AsyncClient, origin: str, ref: str,
                       site: Path, manifest: dict) -> None:
    rel = unquote(ref.split("?")[0].split("#")[0]).lstrip("/")
    if not rel:
        return                                  # that's the site root, not a file
    try:
        resp = await client.get(origin + "/" + rel)
    except httpx.HTTPError:
        return
    if resp.status_code >= 400 or len(resp.content) > _MAX_ASSET_BYTES:
        return
    if "html" in resp.headers.get("content-type", "").lower():
        return                                  # a page link, not an asset
    dest = (site / rel).resolve()
    if dest == site.resolve() or not dest.is_relative_to(site.resolve()):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    manifest["assets"].append("/" + rel)


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------
def _resolve(root: Path, request_path: str) -> Path | None:
    clean = unquote(request_path.split("?")[0].split("#")[0]).lstrip("/")
    rel = clean
    candidates = ["index.html"] if not rel else [rel, rel + ".html",
                                                 f"{rel}/index.html"]
    for candidate in candidates:
        path = (root / candidate).resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
    return None


def _handler_for(site: Path):
    root = site.resolve()

    class MirrorHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:                      # noqa: N802 (stdlib API)
            target = _resolve(root, self.path)
            if target is None:
                self.send_error(404, "not in mirror")
                return
            data = target.read_bytes()
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:
            sys.stdout.write(f"  mirror {self.address_string()} {fmt % args}\n")
            sys.stdout.flush()

    return MirrorHandler


def serve(site: Path, port: int) -> None:
    if not site.is_dir():
        raise SystemExit(f"no mirror at {site} - run `snapshot` first")
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler_for(site))
    print(f"mirror serving {site} at http://127.0.0.1:{port}  "
          f"(loopback only; Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m trafficlab.mirror",
        description="snapshot your own site and serve it locally (loopback only).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("snapshot", help="download pages + assets, strip ad-network tags")
    s.add_argument("origin", help="site to snapshot, e.g. https://your-site.dev")
    s.add_argument("--out", default="mirror/site", help="output directory (default mirror/site)")
    s.add_argument("--max-pages", type=int, default=20, help="cap on pages (default 20)")
    s.add_argument("--max-assets", type=int, default=400, help="cap on assets (default 400)")
    s.add_argument("--allow-hosts", default="",
                   help="comma list of blocklist markers to NOT strip (e.g. your own demo-ad host)")
    s.add_argument("--timeout", type=float, default=20.0)

    v = sub.add_parser("serve", help="serve the mirror from 127.0.0.1")
    v.add_argument("--site", default="mirror/site", help="mirror directory (default mirror/site)")
    v.add_argument("--port", type=int, default=8787)

    args = p.parse_args(argv)
    if args.command == "snapshot":
        allow = [h.strip() for h in args.allow_hosts.split(",") if h.strip()]
        manifest = asyncio.run(snapshot(args.origin, Path(args.out), args.max_pages,
                                        args.max_assets, allow, args.timeout))
        print(f"\nsnapshot done: {len(manifest['pages'])} pages, "
              f"{len(manifest['assets'])} assets, "
              f"{manifest['ad_tags_removed']} ad-network tags stripped")
        print(f"serve it with:  python -m trafficlab.mirror serve --site {args.out}")
        return 0
    serve(Path(args.site), args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
