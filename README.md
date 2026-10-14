# trafficlab

A controlled traffic-simulation lab for a site **you own**. It sends different
kinds of simulated visitors to your site and records how your bot-protection
reacts, so you can learn which signals your protection actually uses.

Built for learning. Only point it at sites you own or have permission to test.

---

## Install

```bash
pip install -r requirements.txt
playwright install chromium        # firefox/webkit optional: playwright install
```

## Quick start

```bash
python gui.py                                                    # control panel (START/STOP buttons)
python run.py --url https://example.com --dry-run               # show the plan, send nothing
python run.py --url https://example.com                        # 60s at 2 sessions/s (asks to confirm)
python run.py --list-profiles                                    # who it can pretend to be
```

## Control panel (`python gui.py`)

A small window: set your URL, pick the bot kind, choose **how many browsers
run in parallel**, **the rate** (sessions/sec), **how many pages each visitor
walks through**, **how long they wait per page**, then press **START BOTS**.
STOP ends the run cleanly and you still get the summary.

- **Bot kind** — "Real browser bots" loads your site with real browser engines
  (JS, ads, images, video all execute). "Header-only HTTP bots" is the cheap
  no-JS traffic. "Mixed" runs both.
- **Pages per session** — how many pages one visitor reads before leaving
  (default 3). Sessions share one cookie jar and follow a real navigation
  path, which is what visitor-style traffic actually looks like.
- **Full page load (ads, images, video all execute)** — on by default for
  browser bots: each page is swept (which fires lazy-loaded ads), then the
  visit waits for the load event (capped, so hanging assets can't stall a
  visit) and watches ad slots/frames fill. Ad requests actually leave the
  browser, so your own ad monitor/CPM dashboard sees real impressions.
- **Returning visitors (remember cookies)** — visitors come back on later
  runs with their cookies/localStorage intact (stored in `personas/`).
- **Real Chrome** — uses your installed Google Chrome instead of Playwright's
  bundled Chromium (consistent, genuine browser fingerprints).
- **Show browser windows** — turns off headless so you can watch the bots
  browse, including the ads loading.
- Raw JSONL + summary still land in `results/`.

**Throughput reality check:** a full-load page takes ~6–12s and a session
spans several pages, so one browser worker sustains roughly
`1/(9 × pages-per-session)` sessions per second. The panel and the plan
output both print the exact worker count you need for your rate, so you
don't just build a backlog.

## Sessions: traffic that looks like visitors

Every arrival is a *session*, not a lone page hit:

- the bot enters on a weighted entry page (home gets more),
- reads it (dwell, scroll, mouse movement, ads load),
- moves through 2–12 more distinct pages of your site (one cookie jar, real
  `Referer` chain between pages, think-time between hops),
- leaves.

Entry is either **direct** (no referrer) or **internal** (arrives from your
own site) — mixed at `--direct-entry-prob` (default 50/50). Referrers are
always real: the browser/httpx set them from the actual previous page. No
third-party referrer is ever faked, no automation signal is hidden — that's
the honest boundary of this tool.

## The adversarial ladder (`--tiers`)

Every profile has a **tier** — a rung of sophistication. You run the rungs
side by side and read the summary's `by tier` block to find the exact level
where your protection starts stopping traffic. That tells you *how deep* your
detector actually reads, which a single all-or-nothing run never can.

| tier | name      | what it is |
|------|-----------|------------|
| t1   | `naive`   | obvious client libraries (`python-requests`, `curl-cli`) |
| t2   | `claimed` | claims to be a crawler on sight (`fake-crawler`) — does your rule reward lying? |
| t3   | `headers` | perfect browser headers, no JS, python TLS stack (`chrome-headers`) |
| t4   | `engine`  | a real browser engine, all automation tells left honest (`desktop-chrome`, ...) |
| t5   | `varied`  | a real engine wearing a **coherent, ordinary fingerprint** (`varied-*`, 25 of them) |

Read the pass-rate per tier. If t1 is blocked but t5 passes, your check is
shallow. If all five are blocked, your protection is reading something real.

```bash
python run.py --url https://... --tiers naive,headers     # cheap tiers only, no browser
python run.py --url https://... --tiers engine,varied     # browser-only comparison
python run.py --url https://... --mode mixed --rate 2     # the full ladder at once
```

### What t5 (`varied`) actually changes

Before t5, every Chromium visit was byte-identical: one User-Agent, one
1920x1080 screen, `en-US`. A detector didn't need to be smart to notice the
whole "population" shared a single resolution — that tell was the *lab's*, not
the bot's. t5 fixes that with **25 coherent fingerprints** (`trafficlab/fingerprints.py`).

"Coherent" is the important word. Real browsers are internally consistent, and
**inconsistency is itself a bot signal** — the classic catches are all
mismatches. So each fingerprint keeps its axes in agreement:

- User-Agent says Windows → Client Hints (`sec-ch-ua-platform`) say Windows too.
- Engine is Chromium → `sec-ch-ua` is present (Chrome always sends it).
- Engine is Firefox/WebKit → `sec-ch-ua` is **absent** (they never send it).
- Locale `ja-JP` → timezone `Asia/Tokyo`, not `America/New_York`.
- DPR matches the hardware: Retina Macs and iPhones get 2-3, Windows/Linux 1.

Critically, the **Chrome/Edge/Firefox/Safari major version is read from the
engine that is actually running** (`resolve_versions`), not hardcoded, so the
UA and Client Hints can never drift from the real browser's own JS. On a
default Playwright Chromium that's currently 151 — a hardcoded UA claiming
130 would be an instant contradiction a detector could catch.

Every knob here is a first-class Playwright context option (`user_agent`,
`viewport`, `device_scale_factor`, `locale`, `timezone_id`, `color_scheme`,
`extra_http_headers`, `is_mobile`, `has_touch`). **Nothing hides automation.**
`navigator.webdriver` is still `true`, the CDP artifacts are still present, the
TLS stack is still the engine's own. t5 makes traffic *varied* so a detector
that groups by fingerprint has something real to group — it does not make it
invisible.

Useful knobs:

```bash
python run.py --url https://... --pages-per-session 5          # longer visits
python run.py --url https://... --persist-sessions             # returning visitors (personas/)
python run.py --url https://... --direct-entry-prob 0.8        # mostly direct entries
python run.py --url https://... --pages-per-session 1          # old behaviour: single page
```

## Local mirror + stealth (localhost only)

This is the setup for testing **your own detector with automation tells
hidden**, end to end on your machine:

```bash
# 1. snapshot your site (pages + assets; ad-network tags are STRIPPED)
python -m trafficlab.mirror snapshot https://your-site.dev --out mirror/site

# 2. serve it from 127.0.0.1 (loopback only)
python -m trafficlab.mirror serve --site mirror/site --port 8787

# 3. run the bots at it with the automation-tell shims on
python run.py --url http://127.0.0.1:8787 --stealth --mode browser
```

`--stealth` shims the classic browser automation tells before any page JS
runs, so your detector is tested on traffic that does *not* hand it the easy
answers:

| tell | honest bots | with `--stealth` | detector question it answers |
|------|-------------|------------------|------------------------------|
| `navigator.webdriver` | `true` | `undefined` | does my rule read webdriver and nothing else? |
| `window.chrome` | missing in headless | present (Chromium UAs only) | does my rule assume the chrome object exists? |
| `navigator.plugins` | `0` | 3 standard entries | does my rule treat "no plugins" as a bot? |

**Enforced scope, not just a promise:** `Settings.validate()` refuses
`--stealth` unless the target host is loopback (`localhost`, `127.x.x.x`,
`::1`). Pointing stealth traffic at any other site exits with an error before
a single request is sent — the flag physically cannot reach Cloudflare,
Adsterra or anything else. Production runs keep every signal honest.

**The mirror is inert by design:** tags pointing at ad networks / trackers
(AdSense, DoubleClick, Adsterra, PropellerAds, Taboola, ...) are removed
during the snapshot, and the server binds 127.0.0.1 only — so bots browsing
the mirror can never generate an impression, click or event at a real
network. Your own self-hosted scripts (like a demo `js/ads.js`) are kept and
run locally. Extend the blocklist or exempt a host with `--allow-hosts`.

Caveats: the snapshot is static — server-side endpoints (`/api/...`, page
functions) 404 on the mirror, so a detector with a backend half needs its API
mirrored or stubbed. Third parties *not* on the blocklist (fonts, CDNs) still
load. `--stealth` and `--persist-sessions` combine: returning visitors with
hidden tells is the hardest honest class for a rule set to catch.

Useful runs:

```bash
# header-only bots only, 30 seconds, no browser needed
python run.py --url https://... --mode http --rate 2 --duration 30

# real browsers only, watch them work
python run.py --url https://... --mode browser --visits 20 --headed

# precise page set, reproducible run
python run.py --url https://... --pages /,/movies,/mylist --seed 42

# lean on the HTTP bots (mixed = both kinds share the traffic)
python run.py --url https://... --mode mixed --browser-ratio 0.25 --rate 5
```

Stop any run with `Ctrl+C` — you still get the summary.

---

## Architecture

```
    arrival process (poisson or constant, --rate sessions/s)
                |
                |  pick entry page (weighted) + build session path
                |  + profile, route by mode
                v
        +---------------+       +--------------+
        | browser queue |       |  http queue  |        unbounded: arrivals are
        +-------+-------+       +------+-------+        *offered* at the set rate,
                |                       |                workers drain at their own
        browser workers           http workers           speed (offered vs completed
                |                       |                 is itself a signal your
                +-----------+-----------+                 protection can act on)
                            v
                     Recorder (JSONL)  ->  results/*.jsonl + summary-*.json
```

| module                 | responsibility |
|------------------------|----------------|
| `trafficlab/config.py` | one `Settings` object: defaults <- `--config file.json` <- CLI flags |
| `trafficlab/discovery.py` | finds pages (sitemap -> link crawl), normalises + weights them |
| `trafficlab/scheduler.py` | arrival process (poisson/constant) + weighted page choice |
| `trafficlab/profiles.py` | the visitor catalog: browser profiles and HTTP profiles, each tagged with a ladder tier |
| `trafficlab/fingerprints.py` | 25 coherent browser fingerprints (UA + Client Hints + viewport + locale + timezone that agree) |
| `trafficlab/behaviors.py` | human-like mouse, scroll, dwell, ad detection, link picking |
| `trafficlab/visitors.py` | one browser session / one HTTP session, plus the workers |
| `trafficlab/metrics.py` | classify outcomes, live stats, JSONL + final summary |
| `trafficlab/runner.py` | wires it all together, owns shutdown and cleanup |
| `trafficlab/cli.py`     | argparse, config merge, confirmation prompt |
| `trafficlab/gui.py`     | the tkinter control panel |
| `trafficlab/mirror.py`  | local mirror: snapshot (ad tags stripped) + loopback server |
| `trafficlab/stealth.py` | automation-tell shims, applied only when `--stealth` passes the loopback gate |

Design choices worth knowing:

- **Two traffic kinds.** `browser` = real Playwright engines (JS runs, real
  rendering). `http` = header-only requests with no JS. `mixed` splits them.
- **Profiles are characters.** Realistic browsers (`desktop-chrome`,
  `mobile-safari`) down to embarrassingly obvious bots (`naive-python`,
  `curl-cli`) and a careful browser-header clone (`chrome-headers`).
- **Navigation waits for `domcontentloaded`, not `load`.** Slow third-party
  assets can hang for minutes; waiting for `load` is what made the original
  script time out forever. The document status is captured from a response
  listener so it is recorded even if the page never fully loads.
- **Full load (`--full-load`, default on) is a second phase.** Once the HTML
  is parsed the visitor sweeps the page (lazy ads fire when they near the
  viewport), then waits for the load event up to `--load-grace-ms` and for ad
  slots/frames up to `--ad-wait-ms`. Each wait is capped so a hanging asset
  can never stall the run. Whether the load event fired and how many ad
  signals were seen is recorded per page (`loaded`, `ads`, plus each
  `chain` entry).
- **Every arrival is a session, not a lone hit.** The producer picks a
  weighted entry page and a path of 2–`--pages-per-session` distinct pages;
  the visitor walks the path in one browser context (one cookie jar), with
  think-time between hops and a real `Referer` chain set by the browser
  itself. Entry is direct (no referrer) or internal (from your own site),
  mixed at `--direct-entry-prob`. With `--persist-sessions` each worker-slot
  keeps a persona in `personas/` so visitors *return* across runs with their
  cookies and history intact.
- **Fresh context per session by default** (separate cookies), or
  `--reuse-context` for one persistent context per worker — persistence is a
  signal your protection may be looking for.
- **Size `--concurrency` to your rate.** A full-load page takes roughly
  9s (nav + settle + dwell) and a session spans several pages, so one browser
  worker sustains only ~`1/(9 × pages-per-session)` sessions/s. If
  `completed` lags `offered` in the summary, add workers. HTTP sessions are
  fast (~1s/page), so a few HTTP workers easily keep up.
- **Deliberately not included:** TLS/JA3 spoofing, CDP fingerprint patching,
  faked third-party referrers (e.g. fake Google), proxy rotation, challenge
  solving. Those are evasion techniques aimed at other people's systems, not
  testing. The t5 `varied` tier changes *which* honest browser shows up (a
  real Chrome 151 at 1080p, a real Safari on a Retina MacBook, ...) but never
  hides that it is automated — `navigator.webdriver` stays `true`. The classic
  automation tells (`webdriver`/`window.chrome`/plugins) can be shimmed for
  local detector tests via `--stealth`, but that flag is loopback-only by
  code-enforced validation — see the mirror section.

---

## Reading the results

Each visit gets an `outcome`:

| outcome     | meaning |
|-------------|---------|
| `ok`        | 2xx/3xx, normal page served |
| `blocked`   | 401/403/407/429/451 with no challenge page — a hard refusal |
| `challenge` | a challenge/captcha page was served (Cloudflare, hCaptcha, reCAPTCHA, ...) |
| `timeout`   | no answer inside `--timeout-ms` |
| `http_error`| other statuses (404, 500, ...) |
| `error`     | transport failure (DNS, reset, browser crash) |

### Experiment ideas for your own protection

1. **Do you trust a User-Agent?**
   `--mode http --profiles naive-python,curl-cli,fake-crawler,chrome-headers`
   If `naive-python` is `blocked` but `chrome-headers` is `ok`, your check is
   mostly header-deep. If all HTTP profiles are `blocked` but browsers pass,
   your JS challenge is doing real work.
2. **Does a crawler claim open the door?**
   `fake-crawler` claims to be a bot on purpose. If it passes while browsers
   get challenged, your protection rewards lying.
3. **Does automation show?** Browser profiles still carry Playwright's
   automation signals. If they pass every time, your protection likely does
   not inspect the browser at all.
4. **Rate vs. repetition.** Raise `--rate` until `blocked` appears — that
   tells you whether you rate-limit at all, and where.
5. **Sessions vs one-shots.** Compare `--pages-per-session 1` against
   `--pages-per-session 5`, and a run with and without `--persist-sessions`:
   multi-page returning visitors with cookie history behave differently from
   one-shot hits, and that is the class most rules under-weight.
6. **Per-page differences.** The summary's `stopped at` line shows which paths
   attract blocks — maybe only the login or admin-ish pages are guarded.
   The `page views` line counts every page in every session, so a mid-session
   block shows up there even when the entry page was `ok`.
7. **Your ad/CPM monitor.** Run `--mode browser --full-load` with `--headed`
   once and watch the ads appear, then run headless and compare the numbers on
   your dashboard against trafficlab's `full load` summary line ("ad signals
   avg N/page view"). If your monitor counts a visit as an impression before the
   ad request actually fired (ad signal 0), it is counting phantom views —
   that is the exact bug this comparison finds.

### Reading the full-load / session fields

Each record also carries:

| field     | meaning |
|-----------|---------|
| `loaded`  | did the page's load event fire inside the grace window (entry page) |
| `ads`     | how many ad slots/frames were observed (entry page; chain pages carry their own) |
| `nav_ms`  | time until the document response (pure navigation) |
| `latency_ms` | whole session, including dwell, scroll and page hops |
| `session_pages` | how many pages the session was built from |
| `page_views` | pages actually completed (entry + chain) |
| `entry_referrer` | `direct` (no referrer) or `internal` (from your own site) |
| `chain[]` | one entry per later page: path, status, nav_ms, loaded, ads, outcome |

`chain` is what makes mid-session blocks visible: if the entry page is `ok`
but page 3 of the session returns `blocked`, the session outcome stays `ok`
and the `page views` line in the summary shows the block.

> **Warning about real ad networks.** Automated impressions are "invalid
> traffic" for Google AdSense and every other real ad network, and they will
> ban an account for it — pointing trafficlab at a page carrying a live ad
> publisher ID is a fast way to lose that account. Test your own ad monitor
> with your own demo/self-hosted ad code, and leave real ad snippets out of
> the pages you run bots against.

### Files written

```
results/visits-20261013-143000.jsonl   one JSON line per visit
results/summary-20261013-143000.json   aggregate: outcomes, per profile, per path, latency
```

Inspect them however you like, e.g.:

```bash
python -c "import json,sys,collections; c=collections.Counter(json.loads(l)['outcome'] for l in open('results/visits-....jsonl')); print(c)"
```

---

## Full flag reference

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

## Safety rails

- Confirmation prompt before any run that is not `--dry-run` (skip with `--yes`).
- Default rate is a polite 2 sessions/s for 60 seconds.
- Output always lands in `results/`, timestamped, never overwrites.
- Returning-visitor state lands in `personas/`, one file per profile+worker.
- Use it only against sites you own or are explicitly allowed to test.

## License

MIT — see [`LICENSE`](LICENSE). Copyright is held by "trafficlab contributors";
this repo is intentionally anonymous (no personal names, sites, or accounts in
the code, docs, or history). Contributions welcome under the same license.
