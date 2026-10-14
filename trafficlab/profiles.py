"""Visitor profiles: the different "characters" the lab can simulate.

Each profile carries a `tier` - its rung on the adversarial ladder. The ladder
is the point of the whole tool: you run every rung at once and read the
summary's `by tier` block to find the exact sophistication level where your
detector starts stopping traffic. A detector that blocks tier-1 but passes
tier-5 is header-deep; one that blocks all five is reading something real.

    tier 1  naive     obvious client libraries (python-requests, curl)
    tier 2  claimed   claims to be a crawler on sight - does your rule reward lying?
    tier 3  headers   perfect browser headers, no JS, python TLS stack
    tier 4  engine    a real browser engine, all automation tells left honest
    tier 5  varied    a real engine wearing a coherent, ordinary-looking
                      fingerprint (UA + Client Hints + viewport + locale +
                      timezone that all agree with each other)

Two families:

* BrowserProfile - a real Playwright browser engine (chromium/firefox/webkit).
  JS executes normally, so these look like ordinary visitors except for
  whatever signals your protection reads (automation flags, TLS stack,
  behaviour patterns).

* HttpProfile - header-only requests through httpx. No JS at all.

Honesty boundary: no TLS/JA3 spoofing and no CDP fingerprint patching, ever.
The classic browser automation tells (navigator.webdriver, window.chrome,
plugins) can be shimmed with --stealth, but that flag is loopback-only by
code-enforced validation - it exists for testing your own detector on a
local mirror (trafficlab/mirror.py), and cannot be aimed at anyone else's
site through this tool. Traffic sent to anything but localhost keeps all
signals honest, which is exactly what makes that traffic informative.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .config import MODES, TIERS
from .fingerprints import FINGERPRINTS, Fingerprint

TIER_NOTES: dict[str, str] = {
    "naive":    "t1  obvious client libraries (python-requests, curl)",
    "claimed":  "t2  claims to be a crawler on sight",
    "headers":  "t3  perfect browser headers, no JS, python TLS stack",
    "engine":   "t4  real browser engine, automation tells left honest",
    "varied":   "t5  real engine wearing a coherent ordinary fingerprint",
}


@dataclass(frozen=True)
class BrowserProfile:
    name: str
    engine: str                      # chromium | firefox | webkit
    viewport: dict[str, int]
    locale: str
    user_agent: str | None = None    # None = keep the engine's own (consistent) UA
    extra_http_headers: dict[str, str] = field(default_factory=dict)
    is_mobile: bool = False
    has_touch: bool = False
    tier: str = "engine"
    fingerprint: Fingerprint | None = None   # varied tier: coherent identity

    def context_kwargs(self) -> dict:
        if self.fingerprint is not None:
            return self.fingerprint.context_kwargs()
        kw: dict = {
            "viewport": dict(self.viewport),
            "locale": self.locale,
        }
        if self.user_agent:
            kw["user_agent"] = self.user_agent
        if self.extra_http_headers:
            kw["extra_http_headers"] = dict(self.extra_http_headers)
        if self.is_mobile:
            kw["is_mobile"] = True
        if self.has_touch:
            kw["has_touch"] = True
        return kw

    def describe(self) -> str:
        if self.fingerprint is not None:
            return f"[{self.tier}] {self.name}  {self.fingerprint.describe()}"
        vp = f"{self.viewport['width']}x{self.viewport['height']}"
        ua = self.user_agent or "(engine default)"
        return f"[{self.tier}] {self.name}  engine={self.engine} viewport={vp} locale={self.locale}"


@dataclass(frozen=True)
class HttpProfile:
    name: str
    headers: dict[str, str]
    tier: str = "headers"


# --------------------------------------------------------------------------
# Tier 4: browser profiles (real engines, JS enabled, tells left honest)
# --------------------------------------------------------------------------
BROWSER_PROFILES: list[BrowserProfile] = [
    BrowserProfile(
        name="desktop-chrome",
        engine="chromium",
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    ),
    BrowserProfile(
        name="desktop-firefox",
        engine="firefox",
        viewport={"width": 1536, "height": 864},
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.5"},
    ),
    BrowserProfile(
        name="desktop-safari",
        engine="webkit",
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    ),
    BrowserProfile(
        name="mobile-safari",
        engine="webkit",
        viewport={"width": 390, "height": 844},
        locale="en-US",
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        is_mobile=True,
        has_touch=True,
    ),
]

# --------------------------------------------------------------------------
# Tier 5: varied - one profile per coherent fingerprint in the catalog.
# Each carries a *different* ordinary-looking identity; the runner re-renders
# them against the live engine version so UA and Client Hints cannot drift.
# --------------------------------------------------------------------------
VARIED_PROFILES: list[BrowserProfile] = [
    BrowserProfile(
        name=f"varied-{fp.name}",
        engine=fp.engine,
        viewport={"width": fp.viewport[0], "height": fp.viewport[1]},
        locale=fp.locale,
        is_mobile=fp.is_mobile,
        has_touch=fp.has_touch,
        tier="varied",
        fingerprint=fp.render("0"),   # placeholder; runner re-renders with the real major
    )
    for fp in FINGERPRINTS
]


# --------------------------------------------------------------------------
# Tier 1-3: HTTP profiles (header-only, no JS)
# --------------------------------------------------------------------------
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

HTTP_PROFILES: list[HttpProfile] = [
    HttpProfile(
        name="naive-python",
        tier="naive",
        headers={
            "User-Agent": "python-requests/2.32.3",
            "Accept": "*/*",
            "Connection": "keep-alive",
        },
    ),
    HttpProfile(
        name="curl-cli",
        tier="naive",
        headers={
            "User-Agent": "curl/8.10.1",
            "Accept": "*/*",
        },
    ),
    HttpProfile(
        name="fake-crawler",
        tier="claimed",
        headers={
            # Classic probe: does the protection trust a crawler claim on sight?
            "User-Agent": "Mozilla/5.0 (compatible; ExampleBot/2.1; +https://example.com/bot)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ),
    HttpProfile(
        name="chrome-headers",
        tier="headers",
        headers={
            # Full browser header set, still no JS and a python TLS stack.
            "User-Agent": _CHROME_UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
        },
    ),
]

ALL_PROFILES: list = [*BROWSER_PROFILES, *VARIED_PROFILES, *HTTP_PROFILES]

BROWSER_PROFILES_BY_NAME: dict[str, BrowserProfile] = {
    p.name: p for p in [*BROWSER_PROFILES, *VARIED_PROFILES]
}
HTTP_PROFILES_BY_NAME: dict[str, HttpProfile] = {p.name: p for p in HTTP_PROFILES}


def names_for(mode: str, tiers: list[str] | None = None) -> list[str]:
    """Profile names that make sense for the given mode (and tiers, if given)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if mode == "browser":
        pool: list = [*BROWSER_PROFILES, *VARIED_PROFILES]
    elif mode == "http":
        pool = list(HTTP_PROFILES)
    else:
        pool = list(ALL_PROFILES)
    if tiers:
        wanted = set(tiers)
        pool = [p for p in pool if p.tier in wanted]
    return [p.name for p in pool]


def filter_by_tiers(profiles: list, tiers: list[str]) -> list:
    if not tiers:
        return profiles
    wanted = set(tiers)
    return [p for p in profiles if p.tier in wanted]


def describe() -> str:
    lines = ["The adversarial ladder - each tier is a rung of sophistication.", ""]
    for tier in TIERS:
        lines.append(f"  {TIER_NOTES[tier]}")
    lines += ["", "Browser profiles (real engines, JS enabled):", ""]
    for p in BROWSER_PROFILES:
        lines.append("  " + p.describe())
        ua = p.user_agent or "(engine default)"
        lines.append(f"    {'':<14} ua={ua[:70]}")
    lines += ["", f"Varied browser profiles ({len(VARIED_PROFILES)} coherent fingerprints):", ""]
    for p in VARIED_PROFILES:
        fp = p.fingerprint
        assert fp is not None
        lines.append(f"  [{p.tier}] {p.name:<28} {fp.describe()}")
        lines.append(f"    {'':<28} engine={fp.engine}")
    lines += ["", "HTTP profiles (header-only, no JS):", ""]
    for p in HTTP_PROFILES:
        lines.append(f"  [{p.tier}] {p.name:<16} ua={p.headers.get('User-Agent', '')[:70]}")
    lines += [
        "",
        "Pick with --profiles desktop-chrome,naive-python  (empty = all valid for --mode)",
        "Or isolate rungs with --tiers engine,varied      (empty = every tier)",
    ]
    return "\n".join(lines)
