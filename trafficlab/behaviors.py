"""Human-like behaviour for browser visits.

Bots that fetch a page and vanish are trivially identifiable. Real visitors
move the mouse, scroll, read for a while and sometimes click through. These
helpers give browser visits a believable dwell pattern - which also gives your
protection something real to observe (time on page, scroll depth, click paths).
"""

from __future__ import annotations

import asyncio
import random
import time

from playwright.async_api import Page

from .config import Settings
from .discovery import normalize_url

# Frame URLs / page markers that indicate an ad slot actually loaded.
AD_FRAME_HINTS = (
    "googlesyndication", "doubleclick", "googleads", "adnxs", "adservice",
    "amazon-adsystem", "adform", "taboola", "outbrain", "criteo", "pubmatic",
)
AD_SLOT_SELECTOR = (
    "ins.adsbygoogle, .adsbygoogle, [data-ad-slot], [data-ad-client], "
    "[id*='google_ads'], [id*='ad-slot'], .ad-slot, [class*='sponsored-']"
)


async def count_ad_signals(page: Page) -> int:
    """How many ad slots/frames are present right now."""
    signals = 0
    try:
        signals += await page.locator(AD_SLOT_SELECTOR).count()
    except Exception:
        pass
    try:
        for frame in page.frames:
            url = frame.url or ""
            if any(hint in url for hint in AD_FRAME_HINTS):
                signals += 1
    except Exception:
        pass
    return signals


async def mouse_move(page: Page, rng: random.Random) -> None:
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        await page.mouse.move(
            rng.uniform(0.1, 0.9) * vp["width"],
            rng.uniform(0.1, 0.9) * vp["height"],
            steps=rng.randint(6, 20),
        )
    except Exception:
        pass


async def scroll(page: Page, rng: random.Random) -> None:
    try:
        for _ in range(rng.randint(2, 5)):
            await page.mouse.wheel(0, rng.randint(200, 700))
            await asyncio.sleep(rng.uniform(0.15, 0.55))
    except Exception:
        pass


async def dwell(rng: random.Random, lo: float, hi: float) -> None:
    await asyncio.sleep(rng.uniform(lo, hi))


async def scroll_through(page: Page, rng: random.Random) -> None:
    """Sweep the whole page, top to bottom.

    This is what makes lazy-loaded ads fire: ad slots only request creatives
    once they come near the viewport.
    """
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        height = await page.evaluate("() => document.body.scrollHeight || 0")
        steps = max(1, min(10, int(height / max(vp["height"], 1))))
        for _ in range(steps):
            await page.mouse.wheel(0, vp["height"])
            await asyncio.sleep(rng.uniform(0.2, 0.5))
        await asyncio.sleep(rng.uniform(0.3, 0.8))
    except Exception:
        pass


async def settle_after_navigation(page: Page, settings: Settings,
                                  rng: random.Random) -> tuple[bool, int]:
    """After the HTML is parsed: sweep the page, wait for `load`, watch for ads.

    Returns (load_event_fired, ad_signals_seen).

    Never blocks forever: `load_grace_ms` is the hard cap, because hanging
    third-party assets can keep the load event from ever firing. We also stop
    early once ads are clearly present - the point of a full load is that the
    ad scripts ran, not that every image finished.
    """
    start = time.monotonic()
    grace = settings.load_grace_ms / 1000.0
    ad_wait = min(settings.ad_wait_ms / 1000.0, grace)
    loaded = False
    loaded_at = 0.0
    ads = 0

    await scroll_through(page, rng)

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= grace:
            break                              # hard cap, assets may hang forever
        if not loaded:
            try:
                await page.wait_for_load_state("load", timeout=0.4)
                loaded = True
                loaded_at = elapsed
            except Exception:
                pass
        try:
            ads = max(ads, await count_ad_signals(page))
        except Exception:
            pass
        if loaded:
            if ads > 0:
                break                          # loaded AND ads fired: done
            if elapsed >= loaded_at + ad_wait:
                break                          # loaded, ads got their window
        if ads > 0 and elapsed >= ad_wait:
            break                              # ads fired but load is stuck on
                                               # hanging assets - don't wait forever
        await asyncio.sleep(0.3)

    return loaded, ads


async def pick_random_link(page: Page, origin: str, rng: random.Random) -> tuple[str, str] | None:
    """Pick one internal link that exists on the current page. Returns (path, url)."""
    try:
        hrefs: list[str] = await page.eval_on_selector_all(
            "a[href]", "els => els.map(a => a.href)"
        )
    except Exception:
        return None

    candidates = []
    for href in hrefs:
        norm = normalize_url(href, origin)
        if norm:
            candidates.append(norm)
    if not candidates:
        return None
    return rng.choice(candidates)
