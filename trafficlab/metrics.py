"""Recording, classifying and summarising visits.

Every visit becomes one JSON line with an `outcome`:

    ok         2xx/3xx, normal page
    blocked    401/403/407/429/451 without a challenge page (hard refusal)
    challenge  a challenge/captcha page was served (JS challenge, captcha, ...)
    timeout    the request never finished inside --timeout-ms
    http_error other statuses (404, 500, 503 without challenge markers, ...)
    error      transport failure (DNS, connection reset, browser crash, ...)

Comparing `ok` vs `blocked`/`challenge` per profile is the whole point: it
shows which kinds of visitor your protection stops and which it lets through.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from collections import Counter
from pathlib import Path

from .config import TIERS
from .discovery import PageTarget

CHALLENGE_MARKERS = (
    "cf-chl",
    "cf_chl",
    "challenge-platform",
    "just a moment",
    "turnstile",
    "hcaptcha",
    "recaptcha",
    "captcha",
    "checking your browser",
    "enable javascript and cookies",
)

_BLOCK_STATUSES = {401, 403, 407, 429, 451}


def _has_challenge_marker(html: str) -> bool:
    if not html:
        return False
    low = html.lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


def classify(status: int | None, error: str | None, html: str) -> str:
    if _has_challenge_marker(html):
        return "challenge"
    if error == "timeout":
        return "timeout"
    if error:
        return "error"
    if status in _BLOCK_STATUSES:
        return "blocked"
    if status is not None and 200 <= status < 400:
        return "ok"
    if status is None:
        return "error"
    return "http_error"


def new_record(mode: str, target: PageTarget, profile: str,
               tier: str | None = None) -> dict:
    return {
        "ts": time.time(),
        "mode": mode,                 # browser | http
        "profile": profile,           # e.g. desktop-chrome, naive-python
        "tier": tier,                 # rung of the adversarial ladder, if known
        "path": target.path,
        "url": target.url,
        "status": None,               # HTTP status of the main document
        "outcome": None,              # see module docstring
        "error": None,
        "nav_ms": None,               # time until the document response arrived
        "latency_ms": None,           # whole session including human-like dwell
        "loaded": None,               # did the load event fire (full_load visits)
        "ads": None,                  # ad slots/frames observed (full_load visits)
        "session_pages": 1,           # how many pages this session was built from
        "page_views": 1,              # pages actually completed (entry + chain)
        "entry_referrer": None,       # direct | internal
        "stealth": False,             # automation-tell shims applied (loopback-only)
        "final_url": None,
        "chain": [],                  # internal links followed during the visit
    }


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def _fmt_counts(counter: Counter, total: int) -> str:
    parts = []
    for key in ("ok", "blocked", "challenge", "timeout", "http_error", "error"):
        n = counter.get(key, 0)
        if n:
            share = f"{n / total * 100:.1f}%" if total else "0%"
            parts.append(f"{key} {n} ({share})")
    for key, n in sorted(counter.items()):
        if key not in ("ok", "blocked", "challenge", "timeout", "http_error", "error"):
            parts.append(f"{key} {n}")
    return ", ".join(parts) or "-"


class Recorder:
    def __init__(self, out_dir: str) -> None:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = Path(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = folder / f"visits-{stamp}.jsonl"
        self.summary_path = folder / f"summary-{stamp}.json"
        self.records: list[dict] = []
        self.offered = 0
        self.arrival_elapsed: float | None = None   # length of the arrival window
        self.started_wall = _dt.datetime.now()
        self.started = time.monotonic()
        self._fh = self.jsonl_path.open("w", encoding="utf-8")
        self._finished = False

    # ---------------- recording ----------------
    def record(self, rec: dict) -> None:
        self.records.append(rec)
        try:
            self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception:
            pass

    # ---------------- live output ----------------
    def live_line(self) -> str:
        elapsed = time.monotonic() - self.started
        n = len(self.records)
        if not n:
            return f"[{elapsed:6.1f}s] waiting for first visit..."
        page_views = sum(1 + len(r.get("chain") or []) for r in self.records)
        outcomes = Counter(r["outcome"] for r in self.records)
        lat = [r["latency_ms"] for r in self.records if r.get("latency_ms")]
        avg = sum(lat) / len(lat) if lat else 0.0
        p95 = _percentile(lat, 95)
        return (
            f"[{elapsed:6.1f}s] sess={n:4d} pv={page_views:4d} "
            f"ok={outcomes.get('ok', 0):4d} "
            f"blocked={outcomes.get('blocked', 0):3d} "
            f"challenge={outcomes.get('challenge', 0):3d} "
            f"timeout={outcomes.get('timeout', 0):3d} "
            f"error={outcomes.get('error', 0):3d} "
            f"avg={avg:6.0f}ms p95={p95:6.0f}ms"
        )

    # ---------------- final summary ----------------
    def build_summary(self, interrupted: bool) -> dict:
        elapsed = max(time.monotonic() - self.started, 0.001)
        arrival_s = max(self.arrival_elapsed or elapsed, 0.001)
        outcomes = Counter(r["outcome"] for r in self.records)
        by_mode: dict[str, Counter] = {}
        by_profile: dict[str, Counter] = {}
        by_tier: dict[str, Counter] = {}
        by_status: Counter = Counter()
        blocked_paths: Counter = Counter()
        page_outcomes: Counter = Counter()
        entry_mix: Counter = Counter()
        page_views = 0
        latencies: list[float] = []
        nav_times: list[float] = []
        loaded_flags: list[bool] = []
        ad_counts: list[int] = []
        for r in self.records:
            by_mode.setdefault(r["mode"], Counter())[r["outcome"]] += 1
            by_profile.setdefault(r["profile"], Counter())[r["outcome"]] += 1
            by_tier.setdefault(r.get("tier") or "unknown", Counter())[r["outcome"]] += 1
            by_status[str(r["status"])] += 1
            if r["outcome"] in ("blocked", "challenge"):
                blocked_paths[r["path"]] += 1
            if r.get("latency_ms"):
                latencies.append(r["latency_ms"])
            if r.get("nav_ms"):
                nav_times.append(r["nav_ms"])
            if r.get("loaded") is not None:
                loaded_flags.append(bool(r["loaded"]))
            if r.get("ads") is not None:
                ad_counts.append(int(r["ads"]))

            # per page-view view: entry + every chained page
            page_views += 1 + len(r.get("chain") or [])
            page_outcomes[r["outcome"]] += 1
            if r.get("entry_referrer"):
                entry_mix[r["entry_referrer"]] += 1
            for c in r.get("chain") or []:
                if c.get("nav_ms"):
                    nav_times.append(c["nav_ms"])
            for c in r.get("chain") or []:
                outcome = c.get("outcome") or "error"
                page_outcomes[outcome] += 1
                if outcome in ("blocked", "challenge"):
                    blocked_paths[c.get("path", "?")] += 1
                if c.get("loaded") is not None:
                    loaded_flags.append(bool(c["loaded"]))
                if c.get("ads") is not None:
                    ad_counts.append(int(c["ads"]))
        return {
            "target": None,  # filled by runner
            "interrupted": interrupted,
            "started_at": self.started_wall.isoformat(timespec="seconds"),
            "elapsed_s": round(elapsed, 1),
            "offered": self.offered,
            "completed": len(self.records),
            "page_views": page_views,
            "page_outcomes": dict(page_outcomes),
            "entry_mix": dict(entry_mix),
            "arrival_s": round(arrival_s, 1),
            "offered_per_s": round(self.offered / arrival_s, 2),
            "completed_per_s": round(len(self.records) / elapsed, 2),
            "outcomes": dict(outcomes),
            "by_mode": {m: dict(c) for m, c in by_mode.items()},
            "by_profile": {p: dict(c) for p, c in by_profile.items()},
            "by_tier": {t: dict(c) for t, c in by_tier.items()},
            "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
            "top_blocked_paths": dict(blocked_paths.most_common(10)),
            "latency_ms": {
                "avg": round(sum(latencies) / len(latencies)) if latencies else 0,
                "p50": round(_percentile(latencies, 50)),
                "p95": round(_percentile(latencies, 95)),
                "max": round(max(latencies)) if latencies else 0,
            },
            "nav_ms": {
                "avg": round(sum(nav_times) / len(nav_times)) if nav_times else 0,
                "p50": round(_percentile(nav_times, 50)),
                "p95": round(_percentile(nav_times, 95)),
                "max": round(max(nav_times)) if nav_times else 0,
            },
            "page_quality": {
                "load_fired": sum(1 for v in loaded_flags if v),
                "load_tracked": len(loaded_flags),
                "ad_signals_total": sum(ad_counts),
                "ad_signals_avg": round(sum(ad_counts) / len(ad_counts), 2) if ad_counts else 0,
                "visits_with_ads": sum(1 for n in ad_counts if n > 0),
            },
            "files": {"visits": str(self.jsonl_path), "summary": str(self.summary_path)},
        }

    def finish(self, interrupted: bool = False, target: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._fh.close()
        except Exception:
            pass
        summary = self.build_summary(interrupted)
        summary["target"] = target
        try:
            self.summary_path.write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
        self._print_summary(summary)

    def _print_summary(self, s: dict) -> None:
        total = s["completed"]
        line = "=" * 62
        print(line)
        print("trafficlab summary" + ("  (interrupted)" if s["interrupted"] else ""))
        print(line)
        print(f"  target        {s['target']}")
        print(f"  elapsed       {s['elapsed_s']}s")
        print(
            f"  traffic       {s['offered']} offered over {s['arrival_s']}s ({s['offered_per_s']}/s) -> "
            f"{s['completed']} completed over {s['elapsed_s']}s ({s['completed_per_s']}/s)"
        )
        print(f"  sessions      {s['completed']} sessions -> {s['page_views']} page views")
        print(f"  page views    {_fmt_counts(Counter(s['page_outcomes']), s['page_views'])}")
        if s.get("entry_mix"):
            total_entry = sum(s["entry_mix"].values()) or 1
            mix = ", ".join(f"{k} {v} ({v / total_entry * 100:.0f}%)"
                            for k, v in sorted(s["entry_mix"].items()))
            print(f"  entry type    {mix}")
        print(f"  outcomes      {_fmt_counts(Counter(s['outcomes']), total)}")
        # the ladder: which rungs your protection stopped, least -> most sophisticated
        if s.get("by_tier"):
            print("  by tier       (adversarial ladder: t1 naive -> t5 varied)")
            ordered = [t for t in TIERS if t in s["by_tier"]]
            ordered += [t for t in sorted(s["by_tier"]) if t not in ordered]
            for tier in ordered:
                counts = s["by_tier"][tier]
                n = sum(counts.values())
                pass_rate = (counts.get("ok", 0) / n * 100) if n else 0.0
                print(f"    {tier:<9} {_fmt_counts(Counter(counts), n)}   -> passed {pass_rate:.0f}%")
        for mode, counts in sorted(s["by_mode"].items()):
            print(f"    by mode {mode:<8} {_fmt_counts(Counter(counts), sum(counts.values()))}")
        for prof, counts in sorted(s["by_profile"].items()):
            print(f"    by prof {prof:<16} {_fmt_counts(Counter(counts), sum(counts.values()))}")
        status_txt = ", ".join(f"{k}: {v}" for k, v in list(s["by_status"].items())[:8])
        print(f"  status codes  {status_txt or '-'}")
        if s["top_blocked_paths"]:
            blocked_txt = ", ".join(f"{p} x{n}" for p, n in s["top_blocked_paths"].items())
            print(f"  stopped at    {blocked_txt}")
        lat = s["latency_ms"]
        nav = s["nav_ms"]
        print(f"  navigation ms avg {nav['avg']}  p50 {nav['p50']}  p95 {nav['p95']}  max {nav['max']}")
        print(f"  visit ms      avg {lat['avg']}  p50 {lat['p50']}  p95 {lat['p95']}  max {lat['max']}  (includes dwell/scroll)")
        pq = s["page_quality"]
        if pq["load_tracked"]:
            print(f"  full load     load event fired {pq['load_fired']}/{pq['load_tracked']} page views, "
                  f"ad signals {pq['ad_signals_total']} total "
                  f"(avg {pq['ad_signals_avg']}/page view, {pq['visits_with_ads']} page views saw ads)")
        print(f"  raw visits    {s['files']['visits']}")
        print(f"  summary json  {s['files']['summary']}")
        print(line)
