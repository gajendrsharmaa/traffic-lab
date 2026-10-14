"""Coherent browser fingerprints: the "varied" rung of the adversarial ladder.

Why this exists
---------------
Before this module, every Chromium visit in the lab was byte-identical: one
User-Agent, one 1920x1080 viewport, `en-US`, one timezone. Your detector does
not need to be sophisticated to notice that the *entire population* of
"visitors" shares a single screen resolution. That is a tell the lab itself
was creating, and it makes the lab a weaker test than it should be.

So each fingerprint here is a *coherent person*: the User-Agent, the Client
Hints headers, the viewport, the device pixel ratio, the locale, the timezone
and the colour scheme all agree with one another, the way they do for a real
browser install. A Windows Chrome user gets a 1080p screen at DPR 1 and
`America/New_York`; a MacBook gets a Retina screen at DPR 2; an iPhone gets
DPR 3 and `Asia/Tokyo` if its locale says Japan.

What "coherent" means, concretely
---------------------------------
Real browsers are internally consistent, and *inconsistency is itself a bot
signal*. The classic catches are all mismatches:

  - User-Agent says Windows, but Client Hints say the platform is Linux.
  - User-Agent says Chrome, but `sec-ch-ua` is missing (Chrome always sends it).
  - Locale says `ja-JP`, but the timezone is `America/New_York`.
  - User-Agent claims a 4-year-old Chrome version; Client Hints claim a
    different one.

This module keeps those axes in agreement on purpose. Note the version point
in particular: the Chrome/Edge/Firefox/Safari *major version is read from the
engine that is actually running* (see `resolve_versions`), not hardcoded, so
the UA and the Client Hints cannot drift apart from the real browser.

The honest boundary (unchanged)
-------------------------------
Every knob used here is a first-class Playwright context option:
`user_agent`, `viewport`, `device_scale_factor`, `locale`, `timezone_id`,
`color_scheme`, `extra_http_headers`, `is_mobile`, `has_touch`.

None of that hides automation. `navigator.webdriver` is still `true`, the CDP
artifacts are still there, `window.chrome` is still shaped the way Playwright
leaves it, and the TLS stack is still the engine's own. Those tells stay
honest so the lab keeps measuring something - see `stealth.py` for the one
case where they are shimmed, which is code-enforced to loopback targets only.

The point of this module is not "make bots invisible". It is "stop every
visitor being the same clone, so a detector that groups by fingerprint has
something real to group".
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

# Engine majors used before the real engines have been launched. They only
# matter for --dry-run output; real runs overwrite them from the live engine.
_FALLBACK_MAJOR = {"chromium": "141", "firefox": "135", "webkit": "18"}


@dataclass(frozen=True)
class Fingerprint:
    """One coherent browser identity, expressed only in Playwright options."""

    name: str
    engine: str                       # chromium | firefox | webkit
    ua_template: str                  # may contain {major}
    viewport: tuple[int, int]
    device_scale_factor: float
    locale: str                       # also drives Accept-Language + navigator.language
    timezone: str
    color_scheme: str = "light"       # light | dark
    is_mobile: bool = False           # not supported by Playwright on firefox
    has_touch: bool = False
    # Client Hints, Chromium-based engines only. Firefox and Safari do not send
    # them, so a Safari profile carrying sec-ch-ua would be incoherent.
    ch_brands: tuple[str, ...] | None = None   # e.g. ("Chromium", "Google Chrome")
    ch_platform: str | None = None             # "Windows" | "macOS" | "Linux" | "Android"
    # concrete values, filled in by render()
    user_agent: str | None = None
    sec_ch_ua: str | None = None
    sec_ch_ua_mobile: str | None = None
    sec_ch_ua_platform: str | None = None

    def render(self, major: str) -> "Fingerprint":
        """Substitute the real engine version into the templates.

        Keeping UA and Client Hints pinned to the version of the browser that
        is actually running is the difference between "a coherent person" and
        "a bot whose headers contradict its own JS".
        """
        ua = self.ua_template.replace("{major}", major)
        sec_ch_ua = None
        if self.ch_brands:
            parts = [f'"{b}";v="{major}"' for b in self.ch_brands]
            parts.append('"Not?A_Brand";v="99"')
            sec_ch_ua = ", ".join(parts)
        return replace(
            self,
            user_agent=ua,
            sec_ch_ua=sec_ch_ua,
            sec_ch_ua_mobile="?1" if self.is_mobile else "?0",
            sec_ch_ua_platform=self.ch_platform,
        )

    def context_kwargs(self) -> dict:
        """Playwright `new_context` kwargs for this identity."""
        width, height = self.viewport
        kw: dict = {
            "viewport": {"width": width, "height": height},
            "device_scale_factor": self.device_scale_factor,
            "locale": self.locale,
            "timezone_id": self.timezone,
            "color_scheme": self.color_scheme,
        }
        if self.user_agent:
            kw["user_agent"] = self.user_agent
        if self.is_mobile:
            kw["is_mobile"] = True
        if self.has_touch:
            kw["has_touch"] = True
        if self.sec_ch_ua:
            kw["extra_http_headers"] = {
                "sec-ch-ua": self.sec_ch_ua,
                "sec-ch-ua-mobile": self.sec_ch_ua_mobile or "?0",
                "sec-ch-ua-platform": f'"{self.ch_platform}"',
            }
        return kw

    def describe(self) -> str:
        w, h = self.viewport
        return (
            f"{self.locale} {self.timezone} {w}x{h}@{self.device_scale_factor:g} "
            f"{self.color_scheme}"
        )


def _fp(name: str, engine: str, ua: str, viewport: tuple[int, int],
        dpr: float, locale: str, timezone: str, **kw) -> Fingerprint:
    return Fingerprint(
        name=name, engine=engine, ua_template=ua, viewport=viewport,
        device_scale_factor=dpr, locale=locale, timezone=timezone, **kw,
    )


_CHROME_UA = (
    "Mozilla/5.0 ({os}) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{major}.0.0.0 Safari/537.36"
)
_EDGE_UA = (
    "Mozilla/5.0 ({os}) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{major}.0.0.0 Safari/537.36 Edg/{major}.0.0.0"
)
_FIREFOX_UA = (
    "Mozilla/5.0 ({os}; rv:{major}.0) Gecko/20100101 Firefox/{major}.0"
)
_SAFARI_UA = (
    "Mozilla/5.0 ({os}) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/{major}.0 Safari/605.1.15"
)
_IOS_SAFARI_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{major}.0 "
    "Mobile/15E148 Safari/604.1"
)
_IOS_CHROME_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/{major}.0.0.0 "
    "Mobile/15E148 Safari/604.1"
)
_ANDROID_CHROME_UA = (
    "Mozilla/5.0 (Linux; Android 14; {device}) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Mobile Safari/537.36"
)
_ANDROID_FIREFOX_UA = (
    "Mozilla/5.0 (Android 14; Mobile; rv:{major}.0) Gecko/{major}.0 Firefox/{major}.0"
)
_SAMSUNG_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/27.0 Chrome/125.0.0.0 Mobile Safari/537.36"
)

# NOTE: .replace(), not .format() - these templates carry {major} as a deferred
# placeholder that only render() fills in, and .format() would try to expand it.
_W = _CHROME_UA.replace("{os}", "Windows NT 10.0; Win64; x64")
_MAC = _CHROME_UA.replace("{os}", "Macintosh; Intel Mac OS X 10_15_7")
_LINUX = _CHROME_UA.replace("{os}", "X11; Linux x86_64")
_M_FIREFOX_W = _FIREFOX_UA.replace("{os}", "Windows NT 10.0; Win64; x64")
_M_FIREFOX_MAC = _FIREFOX_UA.replace("{os}", "Macintosh; Intel Mac OS X 10.15")
_M_FIREFOX_LINUX = _FIREFOX_UA.replace("{os}", "X11; Linux x86_64")
_M_SAFARI_MAC = _SAFARI_UA.replace("{os}", "Macintosh; Intel Mac OS X 10_15_7")

_CHROME_BRANDS = ("Chromium", "Google Chrome")
_EDGE_BRANDS = ("Microsoft Edge", "Chromium")
_SAMSUNG_BRANDS = ("Samsung Internet", "Chromium")


# --------------------------------------------------------------------------
# The catalog. Each entry is a coherent person, not a random soup of values.
# --------------------------------------------------------------------------
FINGERPRINTS: list[Fingerprint] = [
    # ---- Windows desktop ----
    _fp("win-chrome-1080", "chromium", _W, (1920, 1080), 1.0,
        "en-US", "America/New_York", ch_brands=_CHROME_BRANDS, ch_platform="Windows"),
    _fp("win-chrome-768", "chromium", _W, (1536, 864), 1.0,
        "en-US", "America/Chicago", ch_brands=_CHROME_BRANDS, ch_platform="Windows"),
    _fp("win-chrome-1440-dark", "chromium", _W, (2560, 1440), 1.0,
        "en-GB", "Europe/London", color_scheme="dark",
        ch_brands=_CHROME_BRANDS, ch_platform="Windows"),
    _fp("win-chrome-de", "chromium", _W, (1920, 1080), 1.0,
        "de-DE", "Europe/Berlin", ch_brands=_CHROME_BRANDS, ch_platform="Windows"),
    _fp("win-edge-1080", "chromium", _EDGE_UA, (1920, 1080), 1.0,
        "en-US", "America/Denver", ch_brands=_EDGE_BRANDS, ch_platform="Windows"),
    _fp("win-edge-768", "chromium", _EDGE_UA, (1366, 768), 1.0,
        "en-US", "America/New_York", ch_brands=_EDGE_BRANDS, ch_platform="Windows"),
    _fp("win-firefox-1080", "firefox", _M_FIREFOX_W, (1920, 1080), 1.0,
        "en-US", "America/Los_Angeles", color_scheme="dark"),
    _fp("win-firefox-de", "firefox", _M_FIREFOX_W, (1600, 900), 1.0,
        "de-DE", "Europe/Berlin"),

    # ---- macOS desktop (Retina: DPR 2 is the norm) ----
    _fp("mac-safari-1440", "webkit", _M_SAFARI_MAC, (1440, 900), 2.0,
        "en-US", "America/Los_Angeles"),
    _fp("mac-safari-1680", "webkit", _M_SAFARI_MAC, (1680, 1050), 2.0,
        "en-GB", "Europe/London"),
    _fp("mac-safari-ja", "webkit", _M_SAFARI_MAC, (1440, 900), 2.0,
        "ja-JP", "Asia/Tokyo"),
    _fp("mac-chrome-1512", "chromium", _MAC, (1512, 982), 2.0,
        "en-US", "America/New_York", color_scheme="dark",
        ch_brands=_CHROME_BRANDS, ch_platform="macOS"),
    _fp("mac-chrome-1728", "chromium", _MAC, (1728, 1117), 2.0,
        "en-US", "America/Chicago", ch_brands=_CHROME_BRANDS, ch_platform="macOS"),
    _fp("mac-firefox-1440", "firefox", _M_FIREFOX_MAC, (1440, 900), 2.0,
        "fr-FR", "Europe/Paris"),

    # ---- Linux desktop ----
    _fp("linux-chrome-1080-dark", "chromium", _LINUX, (1920, 1080), 1.0,
        "en-US", "America/New_York", color_scheme="dark",
        ch_brands=_CHROME_BRANDS, ch_platform="Linux"),
    _fp("linux-chrome-768", "chromium", _LINUX, (1366, 768), 1.0,
        "de-DE", "Europe/Berlin", ch_brands=_CHROME_BRANDS, ch_platform="Linux"),
    _fp("linux-firefox-1080", "firefox", _M_FIREFOX_LINUX, (1920, 1080), 1.0,
        "en-US", "America/Phoenix"),

    # ---- iOS (webkit; DPR 3 on modern iPhones) ----
    _fp("ios-safari-14pro", "webkit", _IOS_SAFARI_UA, (393, 852), 3.0,
        "en-US", "America/New_York", is_mobile=True, has_touch=True),
    _fp("ios-safari-se", "webkit", _IOS_SAFARI_UA, (375, 667), 2.0,
        "en-US", "America/Los_Angeles", is_mobile=True, has_touch=True),
    _fp("ios-safari-fr-dark", "webkit", _IOS_SAFARI_UA, (393, 852), 3.0,
        "fr-FR", "Europe/Paris", color_scheme="dark",
        is_mobile=True, has_touch=True),
    _fp("ios-chrome-14", "webkit", _IOS_CHROME_UA, (390, 844), 3.0,
        "en-GB", "Europe/London", is_mobile=True, has_touch=True),

    # ---- Android ----
    _fp("android-chrome-pixel", "chromium",
        _ANDROID_CHROME_UA.replace("{device}", "Pixel 7"),
        (412, 915), 2.625, "en-US", "America/New_York",
        is_mobile=True, has_touch=True, ch_brands=_CHROME_BRANDS, ch_platform="Android"),
    _fp("android-chrome-in", "chromium",
        _ANDROID_CHROME_UA.replace("{device}", "SM-A536B"),
        (360, 800), 3.0, "en-IN", "Asia/Kolkata",
        is_mobile=True, has_touch=True, ch_brands=_CHROME_BRANDS, ch_platform="Android"),
    _fp("android-samsung-es", "chromium", _SAMSUNG_UA, (384, 854), 2.75,
        "es-ES", "Europe/Madrid", is_mobile=True, has_touch=True,
        ch_brands=_SAMSUNG_BRANDS, ch_platform="Android"),
    # Playwright has no `is_mobile` on Firefox, so this one carries the mobile
    # signal in its User-Agent and touch support only - which is exactly how
    # Fennec behaves where the API is unavailable.
    _fp("android-firefox-br", "firefox", _ANDROID_FIREFOX_UA, (393, 851), 2.75,
        "pt-BR", "America/Sao_Paulo", has_touch=True),
]

FINGERPRINTS_BY_NAME: dict[str, Fingerprint] = {f.name: f for f in FINGERPRINTS}


def _major_from(version: str | None) -> str:
    """'131.0.6778.33' -> '131'. Falls back when the engine omits a version."""
    if version:
        head = version.split(".", 1)[0].strip()
        if head.isdigit() and head != "0":
            return head
    return ""


def resolve_versions(profiles: list, browsers: dict) -> list:
    """Re-render fingerprinted profiles using each engine's real version.

    Call this *after* the browsers are launched. It reads `browser.version`
    per engine and pins every Chrome/Edge/Firefox/Safari User-Agent and
    `sec-ch-ua` header to the version that is genuinely running, so a
    detector reading Client Hints cannot find a contradiction with the
    engine's own JS.
    """
    majors: dict[str, str] = {}
    for engine, browser in browsers.items():
        try:
            majors[engine] = _major_from(browser.version) or _FALLBACK_MAJOR[engine]
        except Exception:
            majors[engine] = _FALLBACK_MAJOR[engine]

    resolved = []
    for profile in profiles:
        fp = getattr(profile, "fingerprint", None)
        if fp is None:
            resolved.append(profile)
            continue
        major = majors.get(fp.engine) or _FALLBACK_MAJOR[fp.engine]
        resolved.append(replace(profile, fingerprint=fp.render(major)))
    return resolved
