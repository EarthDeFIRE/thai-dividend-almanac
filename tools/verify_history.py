#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_history.py — one-off (re-runnable) audit of the 2016–2025 static layer.

It pulls, per ticker, the daily close series and the dividend events, then
answers two questions the dashboard currently cannot:

    1. was the seed year-end close right?     -> price diff table
    2. was the seed DPS-per-year right?       -> dividend diff table, two bases

Dividend bases, because the seed never said which one it used:
    exdate  : sum of dividends whose EX-DATE falls in year Y
    fiscal  : dividends with an ex-date in Jan–Jun of year Y+1 are pushed back
              into year Y (Thai finals are approved at the AGM ~April for the
              previous financial year). This is a HEURISTIC, not a filing read.

Outputs (written next to this script):
    history_diff.md        human review: every year that disagrees
    history_verified.json  clean {ticker: {prices[10], div_exdate[10], div_fiscal[10]}}

Nothing here is a source of truth by itself. Rows flagged NEEDS-CHECK must be
confirmed against the company IR page or SET XD history before they are used.
No LLM is involved.

Usage:
    python3 verify_history.py                 # all 24 tickers
    python3 verify_history.py SCB KBANK       # a subset
    python3 verify_history.py --basis fiscal  # which basis to write into the json
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SEED_PATH = os.path.join(HERE, "seed_history.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 almanac-verify/1.0"}
CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
         "?period1={p1}&period2={p2}&interval=1d&events=div%7Csplit")
TZ_BKK = dt.timezone(dt.timedelta(hours=7))
YEARS = list(range(2016, 2026))
PRICE_TOL = 0.03   # 3%
DIV_TOL = 0.05     # 5%


def fetch_json(url, retries=3, timeout=30):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:      # noqa: BLE001 - want any transport failure retried
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError("fetch failed %s (%s)" % (url, last))


def pull(symbol):
    p1 = int(dt.datetime(2015, 11, 1, tzinfo=TZ_BKK).timestamp())
    p2 = int(dt.datetime.now(TZ_BKK).timestamp())
    d = fetch_json(CHART.format(sym=urllib.parse.quote(symbol), p1=p1, p2=p2))
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    adj = None
    try:
        adj = res["indicators"]["adjclose"][0]["adjclose"]
    except Exception:
        pass
    series = []
    for i, t in enumerate(ts):
        c = closes[i]
        if c is None:
            continue
        series.append((dt.datetime.fromtimestamp(t, TZ_BKK).date(), c,
                       (adj[i] if adj and adj[i] is not None else c)))
    ev = res.get("events", {})
    divs = [(dt.datetime.fromtimestamp(v["date"], TZ_BKK).date(), v["amount"])
            for v in ev.get("dividends", {}).values()]
    splits = [(dt.datetime.fromtimestamp(v["date"], TZ_BKK).date(), v.get("splitRatio", "?"))
              for v in ev.get("splits", {}).values()]
    return series, sorted(divs), sorted(splits)


def year_end_closes(series):
    out = {}
    for d, close, adjc in series:
        if d.year in YEARS:
            cur = out.get(d.year)
            if cur is None or d > cur[0]:
                out[d.year] = (d, close, adjc)
    return out


def div_by_year(divs, basis):
    out = {y: 0.0 for y in YEARS}
    for d, amt in divs:
        y = d.year
        if basis == "fiscal" and d.month <= 6:
            y -= 1
        if y in out:
            out[y] += amt
    return out


def pct(a, b):
    return None if not b else (a / b - 1) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--basis", choices=["exdate", "fiscal"], default="fiscal")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    seed = json.load(open(SEED_PATH, encoding="utf-8"))["stocks"]
    tickers = args.tickers or list(seed)

    md = ["# History verification · %s" % dt.date.today().isoformat(),
          "",
          "Source: Yahoo Finance daily series + dividend events. Basis for the json: `%s`." % args.basis,
          "Tolerances: price %.0f%%, dividend %.0f%%. Rows inside tolerance are not printed."
          % (PRICE_TOL * 100, DIV_TOL * 100), ""]
    verified, summary = {}, []

    for t in tickers:
        s = seed[t]
        try:
            series, divs, splits = pull(s.get("symbol", t + ".BK"))
        except Exception as e:      # noqa: BLE001
            md += ["## %s" % t, "", "FETCH FAILED: %s" % e, ""]
            summary.append((t, "fetch-failed", 0, 0))
            continue

        ye = year_end_closes(series)
        d_ex = div_by_year(divs, "exdate")
        d_fi = div_by_year(divs, "fiscal")
        chosen = d_fi if args.basis == "fiscal" else d_ex

        verified[t] = {
            "symbol": s.get("symbol", t + ".BK"),
            "prices": [round(ye[y][1], 2) if y in ye else None for y in YEARS],
            "prices_adj": [round(ye[y][2], 2) if y in ye else None for y in YEARS],
            "div_exdate": [round(d_ex[y], 4) for y in YEARS],
            "div_fiscal": [round(d_fi[y], 4) for y in YEARS],
            "splits": [[str(d), r] for d, r in splits],
        }

        bad_p = bad_d = 0
        rows = []
        for i, y in enumerate(YEARS):
            sp, sd = s["prices"][i], s["div"][i]
            vp = ye[y][1] if y in ye else None
            vd = chosen[y]
            dp = pct(vp, sp) if (vp and sp) else None
            dd = pct(vd, sd) if (vd and sd) else None
            flag = []
            if sp and vp is None:
                flag.append("ไม่มีราคาในแหล่ง")
            elif dp is not None and abs(dp) > PRICE_TOL * 100:
                flag.append("ราคาต่าง %+.1f%%" % dp)
            if not sp and vp:
                flag.append("seed ว่างแต่แหล่งมีราคา")
            if sd and vd == 0:
                flag.append("แหล่งไม่มีปันผลปีนี้")
            elif dd is not None and abs(dd) > DIV_TOL * 100:
                flag.append("ปันผลต่าง %+.1f%%" % dd)
            if not flag:
                continue
            if any("ราคา" in f for f in flag):
                bad_p += 1
            if any("ปันผล" in f for f in flag):
                bad_d += 1
            rows.append("| %d | %s | %s | %s | %s | %s |" % (
                y, sp or "-", "%.2f" % vp if vp else "-", sd or "-",
                "%.2f" % vd if vd else "-", " · ".join(flag)))

        md += ["## %s — %s" % (t, s["name"]), ""]
        if splits:
            md += ["> ⚠ พบ split/par change: %s — ราคาในแหล่งเป็น adjusted ส่วน seed อาจไม่ใช่"
                   % ", ".join("%s %s" % (d, r) for d, r in splits), ""]
        if rows:
            md += ["| ปี | seed price | source price | seed DPS | source DPS | ข้อสังเกต |",
                   "|---|---:|---:|---:|---:|---|"] + rows + [""]
        else:
            md += ["ตรงกันทุกปีภายใน tolerance", ""]
        summary.append((t, "ok", bad_p, bad_d))
        time.sleep(args.delay)

    head = ["| ticker | สถานะ | ปีที่ราคาไม่ตรง | ปีที่ปันผลไม่ตรง |", "|---|---|---:|---:|"]
    head += ["| %s | %s | %d | %d |" % r for r in summary]
    md = md[:5] + ["## สรุป", ""] + head + [""] + md[5:]

    open(os.path.join(HERE, "history_diff.md"), "w", encoding="utf-8").write("\n".join(md))
    json.dump({"generated": dt.datetime.now(TZ_BKK).isoformat(timespec="minutes"),
               "basis": args.basis, "years": YEARS, "stocks": verified},
              open(os.path.join(HERE, "history_verified.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("wrote history_diff.md + history_verified.json for %d tickers" % len(summary))
    bad = sum(1 for _, st, p, d in summary if st != "ok" or p or d)
    print("tickers needing review: %d / %d" % (bad, len(summary)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
