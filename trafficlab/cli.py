"""Command line interface for trafficlab."""

from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlsplit

from . import runner
from .config import DISTRIBUTIONS, MODES, TIERS, Settings
from .profiles import describe, names_for


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trafficlab",
        description="Simulate visitors against a site you own to test its bot protection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python run.py --dry-run\n"
            "  python run.py --mode http --rate 2 --duration 30\n"
            "  python run.py --mode browser --visits 50 --concurrency 4 --headed\n"
            "  python run.py --pages /,/about --profiles desktop-chrome,naive-python\n"
            "  python run.py --list-profiles\n"
        ),
    )
    p.add_argument("--url", help="target site you own, e.g. https://example.pages.dev")
    p.add_argument("--config", help="JSON file with Settings fields (CLI flags override it)")
    p.add_argument("--mode", choices=MODES, help="visitor kind (default: mixed)")
    p.add_argument("--rate", type=float, help="visits per second (default: 2)")
    p.add_argument("--dist", dest="distribution", choices=DISTRIBUTIONS,
                   help="arrival pattern (default: poisson)")
    p.add_argument("--visits", type=int, help="total visits to offer")
    p.add_argument("--duration", type=float,
                   help="seconds of arrival time (default: 60 if --visits not given)")
    p.add_argument("--concurrency", "-c", type=int, help="parallel workers (default: 6)")
    p.add_argument("--browser-ratio", type=float,
                   help="share of mixed traffic that is a real browser (default: 0.6)")
    p.add_argument("--pages", help="comma separated paths, e.g. '/,/about,/posts/one' "
                                   "(default: auto-discover)")
    p.add_argument("--max-pages", type=int, help="cap on discovered pages (default: 40)")
    p.add_argument("--profiles", help="comma separated profile names (default: all for mode)")
    p.add_argument("--tiers", help="comma separated rungs of the adversarial ladder to run "
                                   f"({', '.join(TIERS)}); default = every rung")
    p.add_argument("--dwell-min", type=float, help="min seconds reading a page (default: 1)")
    p.add_argument("--dwell-max", type=float, help="max seconds reading a page (default: 4)")
    p.add_argument("--scroll-prob", type=float, help="probability of scrolling (default: 0.8)")
    p.add_argument("--link-prob", type=float,
                   help="probability of following one internal link (default: 0.25)")
    p.add_argument("--seed", type=int, help="random seed for reproducible runs")
    p.add_argument("--pages-per-session", type=int,
                   help="page views per visitor session (default 3; 1 = single page)")
    p.add_argument("--direct-entry-prob", type=float,
                   help="share of sessions arriving with no referrer (default 0.5)")
    p.add_argument("--persist-sessions", action=argparse.BooleanOptionalAction, default=None,
                   help="returning visitors: keep cookies/localStorage across runs "
                        "in --persona-dir (default: off)")
    p.add_argument("--persona-dir", help="where returning-visitor state is stored (default: personas)")
    p.add_argument("--stealth", action=argparse.BooleanOptionalAction, default=None,
                   help="hide common automation tells (webdriver/window.chrome/plugins); "
                        "LOOPBACK targets only - pairs with trafficlab.mirror")
    p.add_argument("--full-load", action=argparse.BooleanOptionalAction, default=None,
                   help="browser mode: sweep the page, wait for the load event and for "
                        "ad slots to fill (default: on)")
    p.add_argument("--load-grace-ms", type=int,
                   help="max wait for the load event (default: 10000)")
    p.add_argument("--ad-wait-ms", type=int,
                   help="max wait for ad slots/frames (default: 6000)")
    p.add_argument("--gui", action="store_true", help="open the control panel window")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--channel", help='chromium channel, e.g. "chrome" to use installed Google Chrome')
    p.add_argument("--reuse-context", action="store_true",
                   help="keep one session per worker instead of a fresh session per visit")
    p.add_argument("--timeout-ms", type=int, help="per-page timeout (default: 15000)")
    p.add_argument("--out-dir", help="output directory (default: results)")
    p.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    p.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--list-profiles", action="store_true", help="show available profiles and exit")
    return p


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _apply_args(settings: Settings, args: argparse.Namespace) -> None:
    for name in ("url", "mode", "rate", "distribution", "visits", "duration",
                 "concurrency", "browser_ratio", "max_pages", "dwell_min",
                 "dwell_max", "scroll_prob", "link_prob", "seed", "timeout_ms",
                 "out_dir", "channel", "load_grace_ms", "ad_wait_ms",
                 "pages_per_session", "entry_direct_prob", "persona_dir"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(settings, name, value)
    if args.full_load is not None:
        settings.full_load = args.full_load
    if args.persist_sessions is not None:
        settings.persist_sessions = args.persist_sessions
    if args.stealth is not None:
        settings.stealth = args.stealth
    if args.pages:
        settings.pages = _split(args.pages)
    if args.profiles:
        settings.profiles = _split(args.profiles)
    if args.tiers:
        settings.tiers = _split(args.tiers)
    if args.headed:
        settings.headless = False
    if args.reuse_context:
        settings.reuse_context = True
    if args.yes:
        settings.yes = True
    if args.dry_run:
        settings.dry_run = True
    # a budget must exist: default to 60s when the user gave neither
    if settings.visits is None and settings.duration is None:
        settings.duration = 60.0


def _check_profiles(settings: Settings) -> str | None:
    if not settings.profiles:
        return None
    valid = set(names_for(settings.mode))
    unknown = [name for name in settings.profiles if name not in valid]
    if unknown:
        return (f"unknown profile(s) for mode {settings.mode}: {', '.join(unknown)}\n"
                f"valid: {', '.join(sorted(valid))}\n"
                f"(run --list-profiles for details)")
    return None


def _confirm(settings: Settings) -> bool:
    host = urlsplit(settings.url).netloc
    page_estimate = settings.estimated_visits * max(1, settings.pages_per_session)
    print(
        f"\nAbout to send ~{settings.estimated_visits} sessions "
        f"(~{page_estimate} page views, {settings.rate:g} sessions/s for "
        f"{settings.duration if settings.duration is not None else 'until visit budget'}"
        f") to {host} using mode={settings.mode}.",
        flush=True,
    )
    try:
        answer = input("Continue? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):      # keep output readable on Windows consoles
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    args = build_parser().parse_args(argv)

    if args.list_profiles:
        print(describe())
        return 0

    if args.gui:
        from .gui import main as gui_main
        return gui_main(prefill_url=args.url)

    try:
        settings = Settings.from_json(args.config) if args.config else Settings()
        _apply_args(settings, args)
        settings.validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    problem = _check_profiles(settings)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    print(f"trafficlab -> {settings.url}  (mode={settings.mode}, "
          f"rate={settings.rate:g}/s, workers={settings.concurrency}, "
          f"seed={settings.seed if settings.seed is not None else 'random'})",
          flush=True)

    if not settings.dry_run and not settings.yes and not _confirm(settings):
        print("aborted, nothing sent.")
        return 1

    try:
        return asyncio.run(runner.run(settings))
    except KeyboardInterrupt:
        print("\naborted.")
        return 130
