"""Orchestration: discovery -> plan -> arrival producer -> workers -> summary.

Layout of a run:

    arrival process (rate, poisson/constant)
            |
            |  picks page (weighted) + profile, routes by mode
            v
     +-------------+          +-------------+
     | browser queue|          |  http queue |
     +------+------+          +------+------+
            |                        |
     browser workers           http workers      (concurrency split)
            |                        |
            +-----------+------------+
                        v
                   Recorder -> results/*.jsonl + summary.json

The queue is unbounded on purpose: arrivals are offered at the requested rate
regardless of how fast workers drain them, so the summary can show offered vs
completed throughput - which is itself a signal your protection can act on
(rate limiting).
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress

from . import discovery, metrics, visitors
from .config import Settings
from .fingerprints import resolve_versions
from .profiles import (
    BROWSER_PROFILES,
    HTTP_PROFILES,
    VARIED_PROFILES,
    BrowserProfile,
    filter_by_tiers,
)
from .scheduler import ArrivalProcess, weighted_page
from .visitors import Job

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None


def split_workers(settings: Settings) -> tuple[int, int]:
    """Return (browser_workers, http_workers) for the requested mode."""
    c = settings.concurrency
    if settings.mode == "browser":
        return c, 0
    if settings.mode == "http":
        return 0, c
    nb = max(1, min(c - 1, round(c * settings.browser_ratio)))
    return nb, c - nb


def select_profiles(settings: Settings) -> tuple[list[BrowserProfile], list]:
    """Filter the profile catalog down to what this run may use."""
    wanted = set(settings.profiles)
    browser = [p for p in (*BROWSER_PROFILES, *VARIED_PROFILES)
               if settings.mode in ("browser", "mixed")]
    http = [p for p in HTTP_PROFILES if settings.mode in ("http", "mixed")]
    if wanted:
        browser = [p for p in browser if p.name in wanted]
        http = [p for p in http if p.name in wanted]
    browser = filter_by_tiers(browser, settings.tiers)
    http = filter_by_tiers(http, settings.tiers)
    return browser, http


def build_plan(settings: Settings, targets: list[discovery.PageTarget],
               browser_profiles: list[BrowserProfile], http_profiles: list,
               nb: int, nh: int) -> str:
    if settings.mode == "browser":
        mode_txt = "browser"
    elif settings.mode == "http":
        mode_txt = "http"
    else:
        mode_txt = f"mixed (browser {settings.browser_ratio:.0%} / http {1 - settings.browser_ratio:.0%})"
    budget = []
    if settings.duration is not None:
        budget.append(f"{settings.duration:g}s")
    if settings.visits is not None:
        budget.append(f"{settings.visits} visits")
    pages_txt = ", ".join(t.path for t in targets[:6])
    if len(targets) > 6:
        pages_txt += f", +{len(targets) - 6} more"

    # show which rungs of the ladder this run exercises
    tier_counts: dict[str, int] = {}
    for p in (*browser_profiles, *http_profiles):
        tier_counts[p.tier] = tier_counts.get(p.tier, 0) + 1
    ladder = ", ".join(f"{t} x{tier_counts[t]}" for t in sorted(tier_counts)) or "-"

    # throughput reality check: a full-load page takes ~6-12s and a session
    # spans several pages, so each worker sustains only a fraction of a session
    notes: list[str] = []
    if settings.mode in ("browser", "mixed") and settings.full_load and nb:
        browser_demand = (settings.rate if settings.mode == "browser"
                          else settings.rate * settings.browser_ratio)
        page_seconds = 9.0
        session_seconds = page_seconds * max(1, settings.pages_per_session)
        capacity = nb / session_seconds
        pv_capacity = capacity * settings.pages_per_session
        if browser_demand > capacity:
            needed = int(browser_demand * session_seconds) + 1
            notes.append(
                f"note         ~{page_seconds:.0f}s/page x {settings.pages_per_session} pages "
                f"= ~{session_seconds:.0f}s/session: {nb} browser workers sustain "
                f"~{capacity:.1f} sessions/s (~{pv_capacity:.1f} page views/s) but you offer "
                f"{browser_demand:.1f}/s -> raise concurrency to ~{needed}"
            )
        else:
            notes.append(
                f"note         {nb} browser workers sustain ~{capacity:.1f} sessions/s "
                f"(~{pv_capacity:.1f} page views/s); you offer {browser_demand:.1f}/s - fits"
            )

    lines = [
        "-" * 62,
        f" target       {settings.url}",
        f" mode         {mode_txt}",
        f" arrival      {settings.distribution} @ {settings.rate:g} sessions/s",
        f" budget       {' + '.join(budget)} -> ~{settings.estimated_visits} sessions offered",
        f" sessions     {settings.pages_per_session} page views/session, "
        f"{settings.entry_direct_prob:.0%} enter directly (no referrer), "
        f"{'cookies persist across runs' if settings.persist_sessions else 'fresh session each visit'}",
        f" workers      {settings.concurrency} ({nb} browser, {nh} http)",
        f" pages        {len(targets)}: {pages_txt}",
        f" profiles     browser: {', '.join(p.name for p in browser_profiles) or '-'}",
        f"              http:    {', '.join(p.name for p in http_profiles) or '-'}",
        f" ladder       {ladder}",
        f" behaviour    dwell {settings.dwell_min:g}-{settings.dwell_max:g}s, "
        f"scroll {settings.scroll_prob:.0%}, follow-link {settings.link_prob:.0%}",
        f" full load    {'on (load event + ad slots, grace ' + str(settings.load_grace_ms) + 'ms)' if settings.full_load else 'off'}",
        f" output       {settings.out_dir}/",
        *([f" stealth      on (webdriver / window.chrome / plugins shims; "
            f"loopback targets only)"] if settings.stealth else []),
        *notes,
        "-" * 62,
    ]
    return "\n".join(lines)


async def _launch_browsers(settings: Settings, profiles: list[BrowserProfile]):
    if async_playwright is None:
        raise RuntimeError("playwright is not installed (pip install -r requirements.txt)")
    pw = await async_playwright().start()
    browsers: dict = {}
    for engine in sorted({p.engine for p in profiles}):
        try:
            kwargs: dict = {"headless": settings.headless}
            if engine == "chromium" and settings.channel:
                kwargs["channel"] = settings.channel
            browsers[engine] = await getattr(pw, engine).launch(**kwargs)
            note = f" (channel={settings.channel})" if engine == "chromium" and settings.channel else ""
            print(f"[browser] launched {engine}{note}", flush=True)
        except Exception as exc:
            print(f"[browser] could not launch {engine}: {exc}", flush=True)
    if not browsers:
        await pw.stop()
        raise RuntimeError(
            "no browser engine could start - run: playwright install chromium"
        )
    return pw, browsers


async def _producer(settings: Settings, targets, arrival: ArrivalProcess,
                    browser_q: asyncio.Queue, http_q: asyncio.Queue,
                    browser_profiles, http_profiles, rng: random.Random,
                    stats: dict) -> int:
    """Emit jobs at the requested arrival rate until a budget runs out."""
    produced = 0
    deadline = (asyncio.get_running_loop().time() + settings.duration
                if settings.duration is not None else None)
    loop = asyncio.get_running_loop()
    while True:
        if settings.visits is not None and produced >= settings.visits:
            break
        delay = arrival.next_delay()
        if deadline is not None and loop.time() + delay > deadline:
            break
        await asyncio.sleep(delay)
        if deadline is not None and loop.time() >= deadline:
            break

        target = weighted_page(targets, rng)
        if settings.mode == "browser":
            mode = "browser"
        elif settings.mode == "http":
            mode = "http"
        else:
            mode = "browser" if rng.random() < settings.browser_ratio else "http"

        # build the session path: entry page + a few more distinct pages
        pages = [target]
        if settings.pages_per_session > 1:
            k = rng.randint(max(1, settings.pages_per_session - 1),
                            settings.pages_per_session)
            others = [t for t in targets if t.path != target.path]
            if k > 1 and others:
                pages += rng.sample(others, min(k - 1, len(others)))
        # entry is either direct (no referrer) or arrives from the site itself
        entry_referrer = (None if rng.random() < settings.entry_direct_prob
                          else settings.url)

        if mode == "browser":
            profile = rng.choice(browser_profiles)
            queue = browser_q
        else:
            profile = rng.choice(http_profiles)
            queue = http_q
        await queue.put(Job(target, profile.name, pages=pages,
                            entry_referrer=entry_referrer))
        produced += 1
        stats["offered"] = produced   # visible even if the run is interrupted
    return produced


async def _live_loop(recorder: metrics.Recorder, stop: asyncio.Event) -> None:
    print(recorder.live_line(), flush=True)
    while not stop.is_set():
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=2.0)
            return
        print(recorder.live_line(), flush=True)


async def run(settings: Settings) -> int:
    settings.validate()
    rng = random.Random(settings.seed)

    # ---- discovery ----
    try:
        targets = await discovery.discover(settings)
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n[interrupted during page discovery, nothing sent]", flush=True)
        return 130
    except Exception as exc:
        print(f"[!] page discovery failed ({exc}); falling back to the home page only",
              flush=True)
        targets = [discovery.PageTarget(path="/", url=settings.url,
                                        weight=settings.home_weight)]
    if not targets:
        print("[!] no pages to hit", flush=True)
        return 2

    browser_profiles, http_profiles = select_profiles(settings)

    # Tiers 1-3 are header-only and tiers 4-5 are browser-only, so a tier filter
    # can legitimately empty one side of a mixed run. That is not an error -
    # narrow to the mode that still has profiles and say so.
    if settings.mode == "mixed":
        if browser_profiles and not http_profiles:
            print(f"[note] tiers {','.join(settings.tiers)} are browser-only; "
                  f"switching mode mixed -> browser", flush=True)
            settings.mode = "browser"
        elif http_profiles and not browser_profiles:
            print(f"[note] tiers {','.join(settings.tiers)} are header-only; "
                  f"switching mode mixed -> http", flush=True)
            settings.mode = "http"

    if settings.mode in ("browser", "mixed") and not browser_profiles:
        if settings.tiers and not set(settings.tiers) & {"engine", "varied"}:
            print(f"[!] tiers {','.join(settings.tiers)} are all header-only; "
                  f"browser mode needs the 'engine' or 'varied' tier", flush=True)
        else:
            print("[!] no browser profiles selected; check --profiles", flush=True)
        return 2
    if settings.mode in ("http", "mixed") and not http_profiles:
        if settings.tiers and not set(settings.tiers) & {"naive", "claimed", "headers"}:
            print(f"[!] tiers {','.join(settings.tiers)} are all browser-only; "
                  f"http mode needs the 'naive', 'claimed' or 'headers' tier", flush=True)
        else:
            print("[!] no http profiles selected; check --profiles", flush=True)
        return 2

    nb, nh = split_workers(settings)
    print(build_plan(settings, targets, browser_profiles, http_profiles, nb, nh),
          flush=True)
    if settings.dry_run:
        print("[dry run] no traffic sent", flush=True)
        return 0

    # ---- setup ----
    recorder = metrics.Recorder(settings.out_dir)
    browser_q: asyncio.Queue = asyncio.Queue()   # unbounded: arrivals are offered, not gated
    http_q: asyncio.Queue = asyncio.Queue()
    arrival = ArrivalProcess(settings.rate, settings.distribution, rng)
    stop = asyncio.Event()
    worker_tasks: list[asyncio.Task] = []
    live_task: asyncio.Task | None = None
    browsers: dict = {}
    pw = None
    stats = {"offered": 0}
    interrupted = False
    fatal: str | None = None

    try:
        if nb:
            pw, browsers = await _launch_browsers(settings, browser_profiles)
            browser_profiles = [p for p in browser_profiles if p.engine in browsers]
            if not browser_profiles:
                raise RuntimeError("no engine available for the selected browser profiles")
            # Pin each varied fingerprint's UA + Client Hints to the version of
            # the engine that is really running, so a detector reading hints
            # cannot find a contradiction with the engine's own JS.
            browser_profiles = resolve_versions(browser_profiles, browsers)

        live_task = asyncio.create_task(_live_loop(recorder, stop))

        for i in range(nb):
            worker_rng = random.Random((settings.seed or 0) * 100003 + i + 1)
            fixed = browser_profiles[i % len(browser_profiles)] if settings.reuse_context else None
            worker_tasks.append(asyncio.create_task(
                visitors.browser_worker(i, browsers, browser_q, settings,
                                        recorder, worker_rng, fixed_profile=fixed)
            ))
        for i in range(nh):
            worker_rng = random.Random((settings.seed or 0) * 200003 + i + 1)
            worker_tasks.append(asyncio.create_task(
                visitors.http_worker(i, http_q, settings, recorder, worker_rng)
            ))

        producer = asyncio.create_task(
            _producer(settings, targets, arrival, browser_q, http_q,
                      browser_profiles, http_profiles, rng, stats)
        )
        try:
            await producer
        except (asyncio.CancelledError, KeyboardInterrupt):
            interrupted = True
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
        finally:
            recorder.arrival_elapsed = time.monotonic() - recorder.started

        if not interrupted:
            for _ in range(nb):
                await browser_q.put(None)
            for _ in range(nh):
                await http_q.put(None)
            try:
                await asyncio.gather(*worker_tasks)
                worker_tasks.clear()
            except (asyncio.CancelledError, KeyboardInterrupt):
                interrupted = True

    except (asyncio.CancelledError, KeyboardInterrupt):
        interrupted = True
    except RuntimeError as exc:
        fatal = str(exc)
    finally:
        stop.set()
        if live_task is not None:
            live_task.cancel()
            with suppress(asyncio.CancelledError):
                await live_task
        for task in worker_tasks:
            task.cancel()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        for browser in browsers.values():
            with suppress(Exception):
                await browser.close()
        if pw is not None:
            with suppress(Exception):
                await pw.stop()
        recorder.offered = stats["offered"]
        recorder.finish(interrupted=interrupted, target=settings.url)

    if fatal:
        print(f"[!] {fatal}", flush=True)
        return 3
    return 130 if interrupted else 0
