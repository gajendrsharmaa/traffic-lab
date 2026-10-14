"""Central configuration object.

Everything the runner needs lives in one `Settings` instance. Values can come
from (in order of precedence):

    1. built-in defaults        (this file)
    2. a JSON config file       (--config config.json)
    3. command line flags       (see cli.py)
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

MODES = ("browser", "http", "mixed")
DISTRIBUTIONS = ("poisson", "constant")
TIERS = ("naive", "claimed", "headers", "engine", "varied")


@dataclass
class Settings:
    # --- what to hit ---
    url: str = ""

    # --- how much traffic ---
    mode: str = "mixed"                 # browser | http | mixed
    rate: float = 2.0                   # visits per second (arrival rate)
    distribution: str = "poisson"       # poisson | constant
    visits: int | None = None           # total visits to offer
    duration: float | None = 60.0       # seconds of arrival time
    concurrency: int = 6                # parallel workers
    browser_ratio: float = 0.6          # share of workers/visits that are browser (mixed mode)

    # --- which pages ---
    pages: list[str] = field(default_factory=list)   # explicit paths; empty = auto-discover
    max_pages: int = 40
    home_weight: float = 3.0            # relative weight of "/" (most sites get home traffic)

    # --- who pretends to be ---
    profiles: list[str] = field(default_factory=list)  # empty = all profiles valid for mode
    tiers: list[str] = field(default_factory=list)     # empty = every rung of the ladder

    # --- how human-like the browser visits behave ---
    dwell_min: float = 1.0              # seconds "reading" a page
    dwell_max: float = 4.0
    scroll_prob: float = 0.8            # probability of scrolling
    link_prob: float = 0.25             # probability of following one internal link (single-page sessions)

    # --- sessions: one visitor sees several pages, like a real person ---
    pages_per_session: int = 3          # page views per session (1 = single page)
    entry_direct_prob: float = 0.5      # share of sessions arriving with no referrer (direct)
    persist_sessions: bool = False      # returning visitors: keep cookies/localStorage across runs
    persona_dir: str = "personas"

    # --- how completely each page is loaded (browser mode) ---
    full_load: bool = True              # wait for load event + ad slots to fill
    load_grace_ms: int = 20_000         # max wait for the load event (slow assets may hang)
    ad_wait_ms: int = 6_000             # max wait for ad slots/frames after loading started

    # --- plumbing ---
    seed: int | None = None             # None = different every run
    headless: bool = True
    channel: str = ""                   # chromium channel, e.g. "chrome" (installed Google Chrome)
    reuse_context: bool = False         # per-worker persistent session instead of per-visit
    timeout_ms: int = 25_000
    out_dir: str = "results"

    # --- local-only extras ---
    stealth: bool = False                # hide common automation tells; LOOPBACK targets only

    # --- control ---
    yes: bool = False                   # skip confirmation prompt
    dry_run: bool = False               # print the plan, send nothing

    # ---------------- validation ----------------
    def validate(self) -> None:
        if not self.url:
            raise ValueError("--url is required (the site you own, e.g. https://example.pages.dev)")
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"url must be an absolute http(s) URL, got: {self.url!r}")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.distribution not in DISTRIBUTIONS:
            raise ValueError(f"distribution must be one of {DISTRIBUTIONS}, got {self.distribution!r}")
        if self.rate <= 0:
            raise ValueError("--rate must be > 0 (visits per second)")
        if self.concurrency < 1:
            raise ValueError("--concurrency must be >= 1")
        if self.mode == "mixed" and self.concurrency < 2:
            raise ValueError("mixed mode needs --concurrency >= 2 (one worker per kind); "
                             "use --mode browser or --mode http for a single kind")
        if not (0.0 <= self.browser_ratio <= 1.0):
            raise ValueError("--browser-ratio must be between 0 and 1")
        if self.visits is not None and self.visits < 1:
            raise ValueError("--visits must be >= 1")
        if self.duration is not None and self.duration <= 0:
            raise ValueError("--duration must be > 0 seconds")
        if self.visits is None and self.duration is None:
            raise ValueError("need a budget: set --visits and/or --duration")
        if self.dwell_min < 0 or self.dwell_max < self.dwell_min:
            raise ValueError("require 0 <= --dwell-min <= --dwell-max")
        if not (0.0 <= self.scroll_prob <= 1.0):
            raise ValueError("--scroll-prob must be between 0 and 1")
        if not (0.0 <= self.link_prob <= 1.0):
            raise ValueError("--link-prob must be between 0 and 1")
        if self.pages_per_session < 1:
            raise ValueError("--pages-per-session must be >= 1")
        if not (0.0 <= self.entry_direct_prob <= 1.0):
            raise ValueError("--direct-entry-prob must be between 0 and 1")
        for t in self.tiers:
            if t not in TIERS:
                raise ValueError(f"unknown tier {t!r}; valid tiers: {', '.join(TIERS)}")
        if self.max_pages < 1:
            raise ValueError("--max-pages must be >= 1")
        if self.home_weight <= 0:
            raise ValueError("--home-weight must be > 0")
        if self.timeout_ms < 1000:
            raise ValueError("--timeout-ms must be >= 1000")
        if self.load_grace_ms < 1000:
            raise ValueError("--load-grace-ms must be >= 1000")
        if self.ad_wait_ms < 0:
            raise ValueError("--ad-wait-ms must be >= 0")
        if self.stealth:
            self._require_loopback()

    def _require_loopback(self) -> None:
        """Hard gate: stealth only runs against a local target.

        This is a code-enforced scope limit, not a policy note. The lab exists
        to test *your* detector on a mirror of *your* site (see
        trafficlab/mirror.py); hiding automation tells from anyone else's
        protection is out of scope and simply not reachable through this flag.
        """
        host = urlsplit(self.url).hostname or ""
        loopback = host.lower() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ValueError(
                "--stealth only runs against loopback targets "
                "(localhost / 127.x.x.x / ::1). It exists to test your own "
                "detector on a local mirror of your site: "
                "`python -m trafficlab.mirror snapshot <url>` then `... serve`, "
                "then run against http://127.0.0.1:8787. Hiding automation "
                "signals from any other site's protection is out of scope."
            )

    # ---------------- helpers ----------------
    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def estimated_visits(self) -> int:
        if self.visits is not None:
            return self.visits
        return int(round(self.rate * (self.duration or 0)))

    @classmethod
    def from_json(cls, path: str | Path) -> "Settings":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"config file {path} must contain a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"unknown config keys in {path}: {', '.join(unknown)}")
        return cls(**raw)
