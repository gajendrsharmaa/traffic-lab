"""The visit implementations: one browser session, one HTTP session.

A *session* is what a real visitor does: arrive on an entry page, read it,
move through several more pages of the site, leave. Sessions share one cookie
jar and arrive either directly (no referrer) or from another page of the same
site - the browser/httpx set the internal referrer chain honestly, nothing is
spoofed.

Workers pull jobs off a queue, run exactly one session, record it and loop.
A `None` job is the shutdown sentinel.
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress
from pathlib import Path

import httpx
from playwright.async_api import Browser, Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from . import behaviors, metrics, stealth
from .config import Settings
from .discovery import PageTarget
from .profiles import (
    BROWSER_PROFILES_BY_NAME,
    HTTP_PROFILES_BY_NAME,
    BrowserProfile,
    HttpProfile,
)

try:                                # http2 only if the optional dependency exists
    import h2  # noqa: F401
    HAS_HTTP2 = True
except ImportError:                 # pragma: no cover
    HAS_HTTP2 = False


class Job:
    __slots__ = ("target", "profile", "pages", "entry_referrer")

    def __init__(self, target: PageTarget, profile: str,
                 pages: list[PageTarget] | None = None,
                 entry_referrer: str | None = None) -> None:
        self.target = target                    # entry page
        self.profile = profile
        self.pages = pages or [target]          # full path of the session
        self.entry_referrer = entry_referrer    # None = direct entry (no referrer)


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def _status_capture(page, captured: dict):
    """Response listener: remember the main document's status, independent of
    the load event (third-party assets can hang, the status still matters)."""
    def _capture(response) -> None:
        try:
            req = response.request
            if req.is_navigation_request() and req.frame == page.main_frame:
                captured["status"] = response.status
        except Exception:
            pass
    return _capture


async def _load_page(page, target: PageTarget, settings: Settings,
                     rng: random.Random, captured: dict,
                     referer: str | None = None) -> dict:
    """Navigate to one page and behave on it (load fully, dwell, look around).

    `referer` is only used for the entry page - later pages pick up the real
    browser referrer chain on their own. Returns a page record including
    `html` (strip it before storing).
    """
    out: dict = {
        "path": target.path, "url": target.url,
        "status": None, "error": None, "loaded": None, "ads": None,
        "nav_ms": None, "final_url": None, "html": "",
    }
    try:
        nav_t0 = time.perf_counter()
        # domcontentloaded: HTML parsed, no waiting on slow subresources
        goto_kwargs: dict = {"wait_until": "domcontentloaded",
                             "timeout": settings.timeout_ms}
        if referer:
            goto_kwargs["referer"] = referer   # own-site referrer, not spoofed
        resp = await page.goto(target.url, **goto_kwargs)
        out["nav_ms"] = round((time.perf_counter() - nav_t0) * 1000)
        out["status"] = resp.status if resp else captured.get("status")

        if settings.full_load:
            # sweep (lazy ads fire), wait for load event + ad slots, capped
            out["loaded"], out["ads"] = await behaviors.settle_after_navigation(
                page, settings, rng
            )
        await behaviors.mouse_move(page, rng)
        if not settings.full_load and rng.random() < settings.scroll_prob:
            await behaviors.scroll(page, rng)
        await behaviors.dwell(rng, settings.dwell_min, settings.dwell_max)

        with suppress(Exception):
            out["final_url"] = page.url
            out["html"] = (await page.content())[:8000]
    except PlaywrightTimeout:
        out["error"] = "timeout"
        out["status"] = out["status"] or captured.get("status")
        with suppress(Exception):
            out["final_url"] = page.url
            out["html"] = (await page.content())[:8000]
    except PlaywrightError as exc:
        out["error"] = f"playwright {type(exc).__name__}: {exc}"[:300]
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


def _finalize_rec(rec: dict, results: list[dict]) -> str:
    """Entry page -> top-level fields, later pages -> chain. Returns html for classify."""
    classify_html = ""
    if not results:
        return classify_html
    entry = results[0]
    rec["status"] = entry.get("status")
    rec["nav_ms"] = entry.get("nav_ms")
    rec["loaded"] = entry.get("loaded")
    rec["ads"] = entry.get("ads")
    rec["final_url"] = entry.get("final_url")
    if entry.get("error") and not rec.get("error"):
        rec["error"] = entry["error"]
    classify_html = entry.get("html") or ""
    chain = []
    for extra in results[1:]:
        item = {k: v for k, v in extra.items() if k != "html"}
        item["outcome"] = metrics.classify(extra.get("status"), extra.get("error"),
                                           extra.get("html") or "")
        chain.append(item)
    rec["chain"] = chain
    rec["page_views"] = len(results)
    if not classify_html:
        classify_html = results[-1].get("html") or ""
    return classify_html


def _error_record(mode: str, job: Job, exc: BaseException) -> dict:
    by_name = (BROWSER_PROFILES_BY_NAME if mode == "browser" else HTTP_PROFILES_BY_NAME)
    profile = by_name.get(job.profile)
    tier = getattr(profile, "tier", None)
    rec = metrics.new_record(mode, job.target, job.profile, tier=tier)
    rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
    rec["outcome"] = "error"
    rec["latency_ms"] = 0
    return rec


def _persona_file(settings: Settings, profile_name: str, worker_id: int) -> str | None:
    if not settings.persist_sessions or not settings.persona_dir:
        return None
    base = Path(settings.persona_dir)
    with suppress(Exception):
        base.mkdir(parents=True, exist_ok=True)
    return str(base / f"{profile_name}-w{worker_id}.json")


# --------------------------------------------------------------------------
# visits
# --------------------------------------------------------------------------
async def browser_visit(browsers: dict[str, Browser], job: Job, profile: BrowserProfile,
                        settings: Settings, rng: random.Random,
                        reused_ctx=None, persona_file: str | None = None) -> dict:
    rec = metrics.new_record("browser", job.target, profile.name, tier=profile.tier)
    rec["session_pages"] = len(job.pages)
    rec["entry_referrer"] = "direct" if not job.entry_referrer else "internal"
    rec["stealth"] = bool(settings.stealth)
    results: list[dict] = []
    t0 = time.perf_counter()
    ctx = reused_ctx
    try:
        created_here = ctx is None
        if ctx is None:
            kwargs = profile.context_kwargs()
            if persona_file and Path(persona_file).exists():
                kwargs["storage_state"] = persona_file   # returning visitor
            try:
                ctx = await browsers[profile.engine].new_context(**kwargs)
            except Exception:                            # corrupt persona file
                kwargs.pop("storage_state", None)
                ctx = await browsers[profile.engine].new_context(**kwargs)
        if created_here and settings.stealth:
            await stealth.apply(ctx)        # automation-tell shims (loopback-only)
        page = await ctx.new_page()
        captured: dict = {}
        page.on("response", _status_capture(page, captured))

        for i, target in enumerate(job.pages):
            results.append(await _load_page(
                page, target, settings, rng, captured,
                referer=job.entry_referrer if i == 0 else None,
            ))

        # single-page sessions may still follow one link found in the DOM
        if len(results) == 1 and rng.random() < settings.link_prob:
            picked = await behaviors.pick_random_link(page, settings.origin, rng)
            if picked:
                extra = PageTarget(path=picked[0], url=picked[1], weight=1.0)
                results.append(await _load_page(page, extra, settings, rng, captured))
    except Exception as exc:
        if not rec.get("error"):
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        if ctx is not None and persona_file:
            with suppress(Exception):
                await ctx.storage_state(path=persona_file)   # remember this visitor
        if ctx is not None and reused_ctx is None:
            with suppress(Exception):
                await ctx.close()

    classify_html = _finalize_rec(rec, results)
    rec["outcome"] = metrics.classify(rec["status"], rec["error"], classify_html)
    return rec


async def http_visit(job: Job, profile: HttpProfile, settings: Settings,
                     rng: random.Random) -> dict:
    rec = metrics.new_record("http", job.target, profile.name, tier=profile.tier)
    rec["session_pages"] = len(job.pages)
    rec["entry_referrer"] = "direct" if not job.entry_referrer else "internal"
    results: list[dict] = []
    t0 = time.perf_counter()
    timeout = httpx.Timeout(settings.timeout_ms / 1000.0, connect=10.0)
    try:
        # one client for the whole session: cookies persist, like a real crawl
        async with httpx.AsyncClient(
            http2=HAS_HTTP2,
            headers=dict(profile.headers),
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            for i, target in enumerate(job.pages):
                headers: dict[str, str] = {}
                if i == 0:
                    if job.entry_referrer:
                        headers["Referer"] = job.entry_referrer
                else:
                    headers["Referer"] = job.pages[i - 1].url   # honest chain
                out: dict = {
                    "path": target.path, "url": target.url,
                    "status": None, "error": None, "loaded": None, "ads": None,
                    "nav_ms": None, "final_url": None, "html": "",
                }
                nav_t0 = time.perf_counter()
                try:
                    resp = await client.get(target.url, headers=headers)
                    out["nav_ms"] = round((time.perf_counter() - nav_t0) * 1000)
                    out["status"] = resp.status_code
                    out["final_url"] = str(resp.url)
                    with suppress(Exception):
                        out["html"] = resp.text[:8000]
                except httpx.TimeoutException:
                    out["error"] = "timeout"
                except httpx.HTTPError as exc:
                    out["error"] = f"httpx {type(exc).__name__}: {exc}"[:300]
                except Exception as exc:
                    out["error"] = f"{type(exc).__name__}: {exc}"[:300]
                results.append(out)
                if i < len(job.pages) - 1:
                    await asyncio.sleep(rng.uniform(0.4, 1.8))   # think time
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000)

    classify_html = _finalize_rec(rec, results)
    rec["outcome"] = metrics.classify(rec["status"], rec["error"], classify_html)
    return rec


# --------------------------------------------------------------------------
# workers
# --------------------------------------------------------------------------
async def browser_worker(worker_id: int, browsers: dict[str, Browser],
                         queue: asyncio.Queue, settings: Settings,
                         recorder, rng: random.Random,
                         fixed_profile: BrowserProfile | None = None) -> None:
    ctx = None
    if fixed_profile is not None:
        try:
            ctx = await browsers[fixed_profile.engine].new_context(
                **fixed_profile.context_kwargs()
            )
            if settings.stealth:
                await stealth.apply(ctx)    # once per context, loopback-only
        except Exception as exc:
            print(f"[worker browser-{worker_id}] context reuse failed "
                  f"({exc}); falling back to per-visit contexts", flush=True)
            ctx = None
            fixed_profile = None
    try:
        while True:
            job = await queue.get()
            try:
                if job is None:
                    return
                profile = fixed_profile or BROWSER_PROFILES_BY_NAME[job.profile]
                persona = _persona_file(settings, profile.name, worker_id)
                rec = await browser_visit(browsers, job, profile, settings, rng,
                                          reused_ctx=ctx, persona_file=persona)
            except Exception as exc:
                rec = _error_record("browser", job, exc)
            finally:
                queue.task_done()
            recorder.record(rec)
    finally:
        if ctx is not None:
            with suppress(Exception):
                await ctx.close()


async def http_worker(worker_id: int, queue: asyncio.Queue, settings: Settings,
                      recorder, rng: random.Random) -> None:
    while True:
        job = await queue.get()
        try:
            if job is None:
                return
            profile = HTTP_PROFILES_BY_NAME[job.profile]
            rec = await http_visit(job, profile, settings, rng)
        except Exception as exc:
            rec = _error_record("http", job, exc)
        finally:
            queue.task_done()
        recorder.record(rec)
