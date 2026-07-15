#!/usr/bin/env python3
"""
Summer 6-Pack 2026 — GitHub Actions pusher.

Queries Commerce7 directly for the campaign window, computes audience/SKU/channel/
daily/totals, and pushes to the JSONbin the dashboard polls. Runs in GitHub Actions
(Python + requests) on a cron — no Mac, no local CSVs. Mirrors summer6pack_2026_actuals.py.

Credentials come from environment (GitHub Actions secrets):
  COMMERCE7_APP_ID, COMMERCE7_SECRET, COMMERCE7_TENANT, JSONBIN_BIN_ID, JSONBIN_ACCESS_KEY
Audience map: ./audiences.json  (or AUDIENCE_MAP_B64 env = base64 of that JSON)

  python3 push_actuals_ci.py            # compute + push
  python3 push_actuals_ci.py --dry-run  # compute + print, no push
Local testing loads ~/george/.env automatically if creds aren't already set.
"""
import argparse, base64, json, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

BUNDLES = ["SUMMERPSC23", "SUMMERCSC23", "SUMMERPCSC23"]
SIZES = {"1": 125, "2": 700, "3": 875, "4": 1500, "5": 200, "6": 600, "7": 800}
TOTAL_SIZE = 4800
PT = timezone(timedelta(hours=-7))          # PDT (July)
START = datetime(2026, 7, 14, tzinfo=PT)
END = datetime(2026, 7, 25, tzinfo=PT)      # exclusive


def load_local_env():
    if os.environ.get("COMMERCE7_APP_ID"):
        return
    envp = Path.home() / "george" / ".env"
    if envp.exists():
        for line in envp.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_audiences():
    b64 = os.environ.get("AUDIENCE_MAP_B64")
    if b64:
        data = json.loads(base64.b64decode(b64))
    else:
        data = json.loads((Path(__file__).parent / "audiences.json").read_text())
    return data["cust2aud"], set(data["members"])


def fetch_window_orders(app, secret, tenant):
    token = base64.b64encode(f"{app}:{secret}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "tenant": tenant}
    orders, cursor = [], "start"
    for _ in range(100):                     # safety cap
        params = {"limit": 50, "cursor": cursor,
                  "orderPaidDate": "btw:2026-07-14|2026-07-26"}   # wide; filtered below
        r = requests.get("https://api.commerce7.com/v1/order",
                         headers=headers, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        arr = data.get("orders") or next((v for v in data.values() if isinstance(v, list)), [])
        orders += arr
        cursor = data.get("cursor")
        if not cursor or not arr:
            break
    return orders


def compute(orders, cust2aud, members):
    def aud_of(cid):
        return cust2aud.get(cid) or ("3" if cid in members else "7")

    lines, tastings = [], 0
    for o in orders:
        if o.get("paymentStatus") != "Paid" or not o.get("orderPaidDate"):
            continue
        t = datetime.fromisoformat(o["orderPaidDate"].replace("Z", "+00:00"))
        if t < START or t >= END:
            continue
        day = t.astimezone(PT).strftime("%Y-%m-%d")
        for it in o.get("items") or []:
            qty = int(it.get("quantity") or 0)
            if it.get("type") == "Tasting":
                tastings += qty
            if it.get("sku") in BUNDLES:
                lines.append({"oid": o.get("id"), "cid": o.get("customerId") or "",
                              "channel": o.get("channel") or "", "sku": it["sku"],
                              "qty": qty, "rev": (it.get("price") or 0) / 100 * qty, "day": day})

    def agg(ls):
        return len({l["oid"] for l in ls}), sum(l["rev"] for l in ls)

    owned = [l for l in lines if l["channel"] in ("Inbound", "Web")]
    audiences = {}
    for k in SIZES:
        txn, rev = agg([l for l in owned if aud_of(l["cid"]) == k])
        audiences[k] = {"txn": txn, "rev": round(rev),
                        "aov": round(rev / txn, 2) if txn else 0.0,
                        "cvr": round(txn / SIZES[k] * 100, 2)}
    ptxn, prev = agg([l for l in lines if l["channel"] == "POS"])
    audiences["POS"] = {"txn": ptxn, "rev": round(prev),
                        "aov": round(prev / ptxn, 2) if ptxn else 0.0,
                        "recip": tastings,
                        "cvr": round(ptxn / tastings * 100, 2) if tastings else None}

    sku = {}
    for s in BUNDLES:
        ls = [l for l in lines if l["sku"] == s]
        sku[s] = {"units": sum(l["qty"] for l in ls), "rev": round(sum(l["rev"] for l in ls))}
    channel = {}
    for ch in ("Inbound", "Web", "POS"):
        txn, rev = agg([l for l in lines if l["channel"] == ch])
        channel[ch] = {"txn": txn, "rev": round(rev)}
    daily = {}
    d = START
    while d < END:
        ds = d.strftime("%Y-%m-%d")
        txn, rev = agg([l for l in lines if l["day"] == ds])
        daily[ds] = {"rev": round(rev), "txn": txn}
        d += timedelta(days=1)
    ttxn, trev = agg(lines)
    owned_txn = len({l["oid"] for l in owned})
    totals = {"txn": ttxn, "rev": round(trev),
              "aov": round(trev / ttxn, 2) if ttxn else 0.0,
              "cvr": round(owned_txn / TOTAL_SIZE * 100, 2)}
    return {"audiences": audiences, "sku": sku, "channel": channel, "daily": daily,
            "totals": totals, "window": {"start": "2026-07-14", "end": "2026-07-25"},
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load_local_env()
    app, secret, tenant = (os.environ.get("COMMERCE7_APP_ID"),
                           os.environ.get("COMMERCE7_SECRET"),
                           os.environ.get("COMMERCE7_TENANT"))
    if not all([app, secret, tenant]):
        sys.exit("Missing COMMERCE7_* env vars")
    cust2aud, members = load_audiences()
    orders = fetch_window_orders(app, secret, tenant)
    rec = compute(orders, cust2aud, members)
    print(json.dumps(rec["totals"]), f"| fetched {len(orders)} orders")
    if a.dry_run:
        print(json.dumps(rec, indent=2))
        return
    bin_id, key = os.environ.get("JSONBIN_BIN_ID"), os.environ.get("JSONBIN_ACCESS_KEY")
    if not bin_id or not key:
        sys.exit("Missing JSONBIN_BIN_ID / JSONBIN_ACCESS_KEY")
    r = requests.put(f"https://api.jsonbin.io/v3/b/{bin_id}",
                     headers={"Content-Type": "application/json", "X-Access-Key": key},
                     data=json.dumps(rec), timeout=30)
    r.raise_for_status()
    print(f"[pushed] {rec['generated_at']} — {rec['totals']['txn']} txn / ${rec['totals']['rev']:,}")


if __name__ == "__main__":
    main()
