#!/usr/bin/env python3
"""
Pull the order history the shipping-promo ROI analysis needs from the
Commerce7 API and write it out in the same CSV schema the local dashboard
produces, so shipping_promo_roi.py runs unchanged against either source.

Credentials come from the environment (GitHub Actions secrets):
  COMMERCE7_APP_ID, COMMERCE7_SECRET, COMMERCE7_TENANT

  python3 shipping_promo_fetch.py --start 2025-05-01 --end 2026-08-17 --out ./data
"""
import argparse
import base64
import csv
import os
import sys
import time
from datetime import datetime, timedelta

import requests

API = "https://api.commerce7.com/v1/order"


def first(d, *keys, default=""):
    """Commerce7 has moved field names around between API versions; probe in order."""
    for k in keys:
        if isinstance(k, (list, tuple)):
            cur = d
            for part in k:
                cur = cur.get(part) if isinstance(cur, dict) else None
                if cur is None:
                    break
            if cur is not None:
                return cur
        elif d.get(k) is not None:
            return d[k]
    return default


def cents(v):
    """Commerce7 returns money as integer cents."""
    try:
        return round(float(v) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def fetch_range(headers, start, end, verbose=True):
    """
    Page through orders month by month. Commerce7 caps a cursor walk, so slicing
    into months keeps each walk short and makes progress restartable.
    """
    out = []
    cur = start
    while cur < end:
        nxt = min((cur.replace(day=1) + timedelta(days=32)).replace(day=1), end)
        cursor, pages, throttled = "start", 0, 0
        while cursor and pages < 400:
            r = requests.get(
                API,
                headers=headers,
                params={
                    "limit": 50,
                    "cursor": cursor,
                    "orderPaidDate": f"btw:{cur:%Y-%m-%d}|{nxt:%Y-%m-%d}",
                },
                timeout=60,
            )
            if r.status_code == 429:
                # Back off and retry the same cursor. Bounded: without a cap and a
                # sleep this spins on the API for as long as the throttle lasts.
                throttled += 1
                if throttled > 8:
                    raise RuntimeError(f"rate limited {throttled}x at {cur:%Y-%m}")
                time.sleep(min(2 ** throttled, 60))
                continue
            throttled = 0
            r.raise_for_status()
            data = r.json()
            arr = data.get("orders") or next(
                (v for v in data.values() if isinstance(v, list)), []
            )
            out += arr
            cursor = data.get("cursor")
            pages += 1
            if not arr:
                break
        if verbose:
            print(f"  {cur:%Y-%m}: {len(out):,} cumulative", flush=True)
        cur = nxt
    return out


ORDER_COLS = [
    "id", "orderNumber", "customerId", "channel", "paymentStatus", "fulfillmentStatus",
    "orderPaidDate", "total", "subTotal", "shippingTotal", "tax", "discountTotal",
    "discountPct", "discountCodes", "itemQty", "salesAssociate", "salesAssociateId",
    "shipToStateCode", "shipToZipCode", "clubTitle",
]
ITEM_COLS = [
    "orderId", "lineItemId", "sku", "productTitle", "type", "quantity", "originalPrice",
    "price", "originalTotal", "priceTotal", "discountTotal", "taxTotal", "vintage",
    "format", "channel", "salesAssociate", "orderPaidDate", "shipToStateCode",
    "paymentStatus",
]


def flatten(orders, verbose=True):
    if verbose and orders:
        print("  sample order keys:", ",".join(sorted(orders[0].keys())), flush=True)

    orows, irows = [], []
    for o in orders:
        ship_to = o.get("shipTo") or o.get("shippingAddress") or {}
        sa = o.get("salesAssociate") or {}
        sa_name = (
            f"{sa.get('firstName','')} {sa.get('lastName','')}".strip()
            if isinstance(sa, dict) else str(sa or "")
        )
        codes = o.get("promotions") or o.get("discountCodes") or []
        if isinstance(codes, list):
            codes = "|".join(
                str(c.get("title") or c.get("code") or "") if isinstance(c, dict) else str(c)
                for c in codes
            )

        state = first(ship_to, "stateCode", "state", default="")
        paid = o.get("orderPaidDate") or ""
        chan = o.get("channel") or ""
        pstat = o.get("paymentStatus") or ""

        orows.append({
            "id": o.get("id", ""),
            "orderNumber": o.get("orderNumber", ""),
            "customerId": first(o, "customerId", ["customer", "id"], default=""),
            "channel": chan,
            "paymentStatus": pstat,
            "fulfillmentStatus": o.get("fulfillmentStatus", ""),
            "orderPaidDate": paid,
            "total": cents(first(o, "total", ["totals", "total"], default=0)),
            "subTotal": cents(first(o, "subTotal", ["totals", "subTotal"], default=0)),
            # shipping is the field this whole analysis turns on — probe widely
            "shippingTotal": cents(first(
                o, "shipping", "shippingTotal", ["totals", "shipping"],
                ["totals", "shippingTotal"], default=0)),
            "tax": cents(first(o, "tax", ["totals", "tax"], default=0)),
            "discountTotal": cents(first(o, "subTotalAfterItemDiscount", "discountTotal",
                                         ["totals", "discount"], default=0)),
            "discountPct": "",
            "discountCodes": codes,
            "itemQty": sum(int(i.get("quantity") or 0) for i in (o.get("items") or [])),
            "salesAssociate": sa_name,
            "salesAssociateId": sa.get("id", "") if isinstance(sa, dict) else "",
            "shipToStateCode": state,
            "shipToZipCode": first(ship_to, "zipCode", "zip", default=""),
            "clubTitle": o.get("clubTitle", "") or "",
        })

        for it in o.get("items") or []:
            q = int(it.get("quantity") or 0)
            price = cents(it.get("price"))
            orig = cents(first(it, "originalPrice", "price", default=0))
            irows.append({
                "orderId": o.get("id", ""),
                "lineItemId": it.get("id", ""),
                "sku": it.get("sku", ""),
                "productTitle": it.get("productTitle") or it.get("title") or "",
                "type": it.get("type", ""),
                "quantity": q,
                "originalPrice": orig,
                "price": price,
                "originalTotal": round(orig * q, 2),
                "priceTotal": round(price * q, 2),
                "discountTotal": cents(it.get("discountTotal")),
                "taxTotal": cents(it.get("tax")),
                "vintage": it.get("vintage", "") or "",
                "format": it.get("format", "") or "",
                "channel": chan,
                "salesAssociate": sa_name,
                "orderPaidDate": paid,
                "shipToStateCode": state,
                "paymentStatus": pstat,
            })
    return orows, irows


def write(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-05-01")
    ap.add_argument("--end", default=None, help="exclusive; default tomorrow")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()

    app, secret, tenant = (
        os.environ.get("COMMERCE7_APP_ID"),
        os.environ.get("COMMERCE7_SECRET"),
        os.environ.get("COMMERCE7_TENANT"),
    )
    if not all([app, secret, tenant]):
        sys.exit("Missing COMMERCE7_APP_ID / COMMERCE7_SECRET / COMMERCE7_TENANT")

    token = base64.b64encode(f"{app}:{secret}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "tenant": tenant}

    start = datetime.strptime(a.start, "%Y-%m-%d")
    end = (datetime.strptime(a.end, "%Y-%m-%d") if a.end
           else datetime.utcnow() + timedelta(days=1))

    print(f"[fetch] {start:%Y-%m-%d} -> {end:%Y-%m-%d}", flush=True)
    orders = fetch_range(headers, start, end)
    print(f"[fetch] {len(orders):,} orders", flush=True)

    orows, irows = flatten(orders)
    os.makedirs(a.out, exist_ok=True)
    write(os.path.join(a.out, "commerce7_orders.csv"), ORDER_COLS, orows)
    write(os.path.join(a.out, "commerce7_order_items.csv"), ITEM_COLS, irows)

    shipped = sum(1 for r in orows if r["shipToStateCode"])
    nonzero_ship = sum(1 for r in orows if r["shippingTotal"] > 0)
    print(f"[write] {len(orows):,} orders / {len(irows):,} items -> {a.out}", flush=True)
    print(f"[check] {shipped:,} with ship-to state, {nonzero_ship:,} with shipping > $0", flush=True)
    if nonzero_ship == 0:
        print("[WARN] no order carried a shipping charge — the shipping field name "
              "probe likely missed. Check the sample order keys above.", flush=True)


if __name__ == "__main__":
    main()
