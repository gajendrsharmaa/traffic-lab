# trafficlab

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A traffic-simulation lab for testing bot-protection and ad-monitoring systems
on sites **you own**. It sends controlled populations of simulated visitors —
real browser engines and header-only HTTP clients — through multi-page
sessions, and records how your protection responds at every step: per visit,
per page, per profile.

The goal is measurement, not evasion. Traffic carries honest signals unless
you explicitly opt into the loopback-only `--stealth` mode (see
[Local mirror & stealth](#local-mirror--stealth)).

**Scope:** authorized testing of your own or explicitly permitted sites only.

## Install

```bash
pip install -r requirements.txt
playwright install chromium        # firefox/webkit optional: playwright install
```

## Quick start

```bash
python gui.py                                            # control panel (START/STOP)
python run.py --url https://example.com --dry-run        # show the plan, send nothing
python run.py --url https://example.com                 # 60s at 2 sessions/s (confirms first)
python run.py --list-profiles                            # visitor catalog
```

Stop any run with `Ctrl+C` — the summary is still produced.

## Control panel (`python gui.py`)

One window: URL, bot kind, parallel workers, rate, pages per session, wait per
page, **START / STOP** (STOP still produces the summary).

- **Bot kind** — real browser engines (JS, ads, images, video execute),
  header-only HTTP, or mixed.
- **Full page load** (default on) — sweeps each page so lazy-loaded ads fire,
  then waits (capped) for the load event and ad slots. Ad requests genuinely
  leave the browser, so your own ad monitor sees the traffic.
- **Returning visitors** — cookie/localStorage state kept in `personas/` so
  visitors persist across runs.
- **Real Chrome** — installed Google Chrome instead of bundled Chromium.
- **Show windows** — non-headless; **Stealth** — automation-tell shims,
  loopback targets only.

Throughput: a full-load page takes ~9 s, so one browser worker sustains
≈ `1/(9 × pages-per-session)` sessions/s. The panel and `--dry-run` both print
the worker count your chosen rate requires.

## What it sends

### Modes

| mode | what runs |
|------|-----------|
| `browser` | real Playwright engines — JS executes, pages render |
| `http` | header-only requests, no JS — cheap, high volume |
| `mixed` | both, split by `--browser-ratio` |

### Sessions

Every arrival is a session, not a single page hit: a weighted entry page
(home gets more traffic), dwell with scroll/mouse behaviour, then hops across
2–`--pages-per-session` distinct pages in one context — one cookie jar, a real
`Referer` chain set by the browser itself, think-time between hops. Entry is
**direct** (no referrer) or **internal** (from your own site), mixed at
`--direct-entry-prob` (default 50/50). `--persist-sessions` keeps personas in
`personas/` so visitors return across runs.

### Profiles and tiers

Profiles are graded on a five-rung ladder. Run rungs side by side and read the
summary's `by tier` block to find the exact level where your protection starts
stopping traffic — the measurement a single all-or-nothing run cannot give you.

| tier | name | description |
|------|------|-------------|
| t1 | `naive` | obvious client libraries (`python-requests`, `curl-cli`) |
| t2 | `claimed` | claims to be a crawler on sight — does your rule reward lying? |
| t3 | `headers` | perfect browser headers, no JS, python TLS stack |
| t4 | `engine` | real browser engine, all automation tells left honest |
| t5 | `varied` | real engine with one of 25 coherent fingerprints (UA, Client Hints, viewport, locale and timezone always agree; browser version read from the running engine) |

t5 varies *which* ordinary browser shows up; it never hides automation —
`navigator.webdriver` stays `true`.

```bash
python run.py --url https://example.com --tiers naive,headers   # cheap tiers, no browser
python run.py --url https://example.com --tiers engine,varied   # browser-only comparison
python run.py --url https://example.com --mode mixed            # full ladder
```

## Examples

```bash
# 30s of header-only traffic, no browser required
python run.py --url https://example.com --mode http --rate 2 --duration 30

# browser sessions, watch them work
python run.py --url https://example.com --mode browser --visits 20 --headed

# fixed page set, reproducible run
python run.py --url https://example.com --pages /,/movies,/mylist --seed 42

# browser-heavy mix at a higher rate
python run.py --url https://example.com --mode mixed --browser-ratio 0.25 --rate 5

# longer visits, returning visitors, mostly direct entries
python run.py --url https://example.com --pages-per-session 5 --persist-sessions --direct-entry-prob 0.8
```

### Common tests against your own protection

- **Do you trust a User-Agent?** Run `--mode http` across the header profiles.
  If `naive-python` is blocked but `chrome-headers` passes, the check is
  header-deep.
- **Does a crawler claim open the door?** `fake-crawler` announces itself. If
  it passes while browsers are challenged, the rule rewards lying.
- **Does the browser get inspected?** Browser profiles keep honest automation
  signals. If they pass every time, nothing inspects the browser.
- **Where is the rate limit?** Raise `--rate` until `blocked` appears.
- **Does session shape matter?** Compare `--pages-per-session 1` vs `5`, and
  runs with/without `--persist-sessions`.
- **Does your ad monitor count phantom views?** Run headed with `--full-load`,
  then compare the summary's `ad signals avg N/page view` against your
  dashboard. A counted impression with `ads 0` means the monitor fires before
  the ad request does.

## Results

Each visit gets an `outcome`:

| outcome | meaning |
|---------|---------|
| `ok` | 2xx/3xx, normal page served |
| `blocked` | 401/403/407/429/451 without a challenge — a hard refusal |
| `challenge` | a challenge/captcha page was served |
| `timeout` | no answer within `--timeout-ms` |
| `http_error` | other statuses (404, 500, ...) |
| `error` | transport failure (DNS, reset, browser crash) |

Files (timestamped, never overwritten):

```
results/visits-<ts>.jsonl    one JSON line per visit
results/summary-<ts>.json    aggregates: outcomes, per profile, per path, per tier, latency
```

Key record fields:

| field | meaning |
|-------|---------|
| `outcome` | session outcome (entry page) |
| `page_views` | pages actually completed |
| `session_pages` | pages the session was built from |
| `chain[]` | per later page: path, status, nav_ms, loaded, ads, outcome |
| `entry_referrer` | `direct` or `internal` |
| `loaded` / `ads` | load event fired; ad slots/frames observed |
| `nav_ms` / `latency_ms` | navigation time / whole session |

`chain` makes mid-session blocks visible: an `ok` session whose page 3 was
`blocked` still shows the block under `page views`.

> **Real ad networks:** automated impressions are invalid traffic and get
> accounts banned. Run only against pages carrying your own or demo ad code —
> never against pages with a live ad publisher ID.

## Local mirror & stealth

Test your own detector with automation tells hidden, end to end on your
machine:

```bash
# 1 — snapshot your site (ad-network/tracker tags are stripped)
python -m trafficlab.mirror snapshot https://example.com --out mirror/site

# 2 — serve it from 127.0.0.1 (loopback only)
python -m trafficlab.mirror serve --site mirror/site --port 8787

# 3 — bots with automation-tell shims on
python run.py --url http://127.0.0.1:8787 --stealth --mode browser
```

`--stealth` applies three shims before any page JS runs:

| tell | honest bots | with `--stealth` |
|------|-------------|------------------|
| `navigator.webdriver` | `true` | `undefined` |
| `window.chrome` | missing in headless | present (Chromium UAs) |
| `navigator.plugins` | `0` | 3 standard entries |

**Enforced, not promised:** `Settings.validate()` refuses `--stealth` unless
the target host is loopback (`localhost`, `127.x.x.x`, `::1`) — the flag
cannot be aimed at any other host, and validation runs before the first
request.

Mirror notes: ad-network/tracker tags (AdSense, DoubleClick, Adsterra, ...) are
removed at snapshot time and the server binds 127.0.0.1 only, so mirror traffic
can never produce an impression at a real network; the snapshot is static, so
server-side endpoints (`/api/...`) 404 — stub them if your detector has a
backend; adjust the blocklist with `--allow-hosts`.

Not included anywhere in this tool: TLS/JA3 spoofing, CDP fingerprint patching,
faked third-party referrers, proxy rotation, challenge solving.

## CLI reference

```
--url URL                target site (required)
--config FILE            JSON file with Settings fields (CLI overrides it)
--mode browser|http|mixed
--rate N                 sessions per second (default 2)
--dist poisson|constant  arrival pattern (default poisson)
--visits N / --duration S   budget: give either or both
--concurrency N          parallel workers (default 6; mixed needs >= 2)
--browser-ratio R        share of mixed traffic that is a browser (default 0.6)
--pages '/,/about,/x'    explicit pages (default: auto-discover via sitemap/crawl)
--max-pages N            cap on discovered pages (default 40)
--profiles a,b,c         restrict profiles (default: all valid for the mode)
--tiers a,b              restrict to ladder rungs (naive,claimed,headers,engine,varied;
                         default: all)
--dwell-min/--dwell-max  seconds "reading" a page (default 1-4)
--scroll-prob P          chance of scrolling (default 0.8)
--link-prob P            chance of following one internal link when a session is a single page (default 0.25)
--pages-per-session N    page views per visitor session (default 3; 1 = single page)
--direct-entry-prob P    share of sessions arriving with no referrer (default 0.5)
--persist-sessions / --no-persist-sessions   returning visitors, state kept in --persona-dir
--persona-dir DIR        returning-visitor state (default personas)
--stealth / --no-stealth hide common automation tells (webdriver/window.chrome/plugins);
                         LOOPBACK targets only, pairs with trafficlab.mirror
--seed N                 reproducible run
--full-load / --no-full-load   wait for load event + ad slots in browser mode (default: on)
--load-grace-ms N        hard cap on waiting for the load event (default 20000)
--ad-wait-ms N           window for ad slots/frames to appear (default 6000)
--headed                 watch the browser window
--channel chrome         use installed Google Chrome instead of bundled Chromium
--reuse-context          persistent session per worker
--timeout-ms N           per-navigation timeout (default 25000)
--out-dir DIR            output directory (default results)
--dry-run                print the plan, send nothing
--yes                    skip the confirmation prompt
--gui                    open the control panel instead of running
--list-profiles          show the visitor catalog
```

## Safety

- Confirmation prompt before any run that is not `--dry-run` (skip: `--yes`).
- Polite defaults: 2 sessions/s for 60 seconds.
- Output lands in `results/`, timestamped; personas in `personas/`.
- `--stealth` is rejected on any non-loopback target.
- Use only against sites you own or are explicitly allowed to test.

## License

MIT — see [`LICENSE`](LICENSE). Copyright "trafficlab contributors";
this repository is intentionally anonymous. Contributions welcome under the
same license.
