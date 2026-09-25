#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_snapshot.py — daily price + valuation refresh for the Thai Dividend Almanac.

NO LLM IS USED ANYWHERE IN THIS PATH. Prices come from a deterministic quote
endpoint; every ratio is plain arithmetic on fundamentals.json. An LLM cannot
be trusted to emit a price, so it is not in the loop (FM-001 lesson).

    price      <- quote endpoint            (changes daily)
    eps/bvps   <- fundamentals.json         (changes quarterly, by hand)
    P/E        =  price / eps_ttm
    P/BV       =  price / bvps
    Yield      =  dps_ttm / price * 100
    yldCalc    =  (dps_recurring or dps_ttm) / price * 100   <- used by the calculator
    Payout     =  computed in the dashboard as yldCalc * P/E

Guards (a bad number must never reach the page silently):
    * price must be > 0 and the quote timestamp must be <= --max-stale-days old
    * |price / prevClose - 1| must be <= --max-move (default 15%)
    * on any failure the previous snapshot value is kept and the ticker is
      listed in "rejected" / "stale", and the process exits non-zero so the
      scheduler surfaces it.

Usage:
    python3 update_snapshot.py                    # write ../snapshot.json
    python3 update_snapshot.py --dry-run          # print, write nothing
    python3 update_snapshot.py --selftest         # offline fixture, no network
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
FUND_PATH = os.path.join(HERE, "fundamentals.json")
OUT_PATH = os.path.abspath(os.path.join(HERE, "..", "snapshot.json"))

QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
INDEX_SYMBOL = "^SET"          # set to "" to skip the index line
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 almanac-updater/1.0"}
TZ_BKK = dt.timezone(dt.timedelta(hours=7))


def log(msg):
    print(msg, file=sys.stderr)


def fetch_json(url, retries=3, timeout=20):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("fetch failed: %s (%s)" % (url, last))


def quote(symbol, fetch=fetch_json):
    """Return (price, prev_close, as_of_date_str) from the chart endpoint."""
    d = fetch(QUOTE_URL.format(sym=urllib.parse.quote(symbol)))
    meta = d["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    ts = meta.get("regularMarketTime")
    if not ts:
        raise ValueError("no timestamp")
    as_of = dt.datetime.fromtimestamp(ts, TZ_BKK).date().isoformat()
    return price, prev, as_of


def load_prev_snapshot():
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"stocks": {}}


def build(fund, prev, fetch, args):
    today = dt.datetime.now(TZ_BKK).date()
    out, rejected, stale, failed = {}, [], [], []

    for ticker, f in fund["stocks"].items():
        keep = prev.get("stocks", {}).get(ticker)
        try:
            price, prev_close, as_of = quote(f["symbol"], fetch)
        except Exception as e:
            failed.append("%s (%s)" % (ticker, e))
            if keep:
                out[ticker] = dict(keep, src=keep.get("src", "?"), stale=True)
            continue

        age = (today - dt.date.fromisoformat(as_of)).days
        bad = None
        if not price or price <= 0:
            bad = "price<=0"
        elif age > args.max_stale_days:
            bad = "quote %s วัน old" % age
        elif prev_close and abs(price / prev_close - 1) > args.max_move:
            bad = "move %.1f%% > %.0f%%" % ((price / prev_close - 1) * 100, args.max_move * 100)

        if bad:
            (stale if "old" in bad else rejected).append("%s %s (%s)" % (ticker, price, bad))
            if keep:
                out[ticker] = dict(keep, stale=True)
            continue

        eps, bvps = f.get("eps_ttm"), f.get("bvps")
        dps, dps_rec = f.get("dps_ttm"), f.get("dps_recurring")
        row = {
            "price": round(price, 2),
            "prevClose": round(prev_close, 2) if prev_close else None,
            "asOf": as_of,
            "src": "YF",
            "pe": round(price / eps, 2) if eps else None,
            "pb": round(price / bvps, 2) if bvps else None,
            "yld": round(dps / price * 100, 2) if dps else None,
        }
        if dps_rec:
            row["yldCalc"] = round(dps_rec / price * 100, 2)
        for k in ("note", "info"):
            if f.get(k):
                row[k] = f[k]
        out[ticker] = row
        time.sleep(args.delay)

    market = {}
    if INDEX_SYMBOL and not args.no_index:
        try:
            p, _, as_of = quote(INDEX_SYMBOL, fetch)
            market = {"set_close": round(p, 2), "set_close_date": as_of}
        except Exception as e:
            log("index fetch failed: %s (ปล่อยว่าง — หน้าเว็บจะใช้ค่าเดิมพร้อมวันที่เดิม)" % e)

    snap = {
        "generated": dt.datetime.now(TZ_BKK).isoformat(timespec="minutes"),
        "source": "Yahoo Finance chart endpoint · valuation recomputed from fundamentals.json (%s)"
                  % fund.get("updated"),
        "stocks": out,
    }
    if market:
        snap["market"] = market
    return snap, rejected, stale, failed


FIXTURE = {  # --selftest: two good rows, one 4x spike, one stale quote
    "SCB.BK": (158.0, 156.5, 0),
    "KBANK.BK": (1032.0, 258.0, 0),
    "AP.BK": (8.05, 7.95, 0),
    "ICHI.BK": (13.9, 13.8, 9),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-index", action="store_true")
    ap.add_argument("--max-move", type=float, default=0.15)
    ap.add_argument("--max-stale-days", type=int, default=5)
    ap.add_argument("--delay", type=float, default=0.8)
    args = ap.parse_args()

    with open(FUND_PATH, encoding="utf-8") as f:
        fund = json.load(f)

    if args.selftest:
        args.no_index, args.delay = True, 0
        today = dt.datetime.now(TZ_BKK).date()

        def fake(url):
            sym = urllib.parse.unquote(url.split("/chart/")[1].split("?")[0])
            if sym not in FIXTURE:
                raise RuntimeError("no fixture for " + sym)
            price, prev, age = FIXTURE[sym]
            ts = dt.datetime.combine(today - dt.timedelta(days=age),
                                     dt.time(16, 30), TZ_BKK).timestamp()
            return {"chart": {"result": [{"meta": {"regularMarketPrice": price,
                                                   "chartPreviousClose": prev,
                                                   "regularMarketTime": int(ts)}}]}}
        fetch = fake
    else:
        fetch = fetch_json

    prev = load_prev_snapshot()
    snap, rejected, stale, failed = build(fund, prev, fetch, args)

    log("ok=%d rejected=%s stale=%s failed=%s" % (len(snap["stocks"]), rejected, stale, failed))
    if args.dry_run or args.selftest:
        print(json.dumps(snap, ensure_ascii=False, indent=1)[:1400])
    else:
        tmp = OUT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        os.replace(tmp, OUT_PATH)
        log("wrote %s" % OUT_PATH)

    return 1 if (rejected or stale or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
