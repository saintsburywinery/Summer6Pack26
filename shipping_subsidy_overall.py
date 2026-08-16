#!/usr/bin/env python3
"""
Total DTC shipping subsidy — all shipped orders, not just the $1 case promo.

Takes the Commerce7 rate card at face value as the counterfactual: whatever a
shipment would have been billed at standard rates is treated as the true cost
basis, and anything not collected from the customer is the subsidy.

The rate card is not published anywhere machine-readable, so it is recovered
from the orders that actually paid full freight. Commerce7 rate tables emit
discrete prices, so the MODE of full-freight charges for a given
(season, bottle-count, destination) is the standard rate — a median would be
dragged down by partially-discounted orders.

Compares Jun 1 - Aug 16 2026 against the same window in 2025.

  python3 shipping_subsidy_overall.py --data-dir ./_data
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

NON_WINE = "TASTING|TASTE|GIFT CARD|MEMBERSHIP|SHIPPING|EVENT|TICKET|MERCH"

# Charges at or below this are comped or promotional, never full freight, so
# they must not contribute to the recovered rate card.
FULL_FREIGHT_MIN = 2.00
SUMMER_MONTHS = {6, 7, 8, 9}


def bucket(n):
    if n <= 0:
        return "0"
    if n <= 3:
        return "1-3"
    if n <= 6:
        return "4-6"
    if n <= 11:
        return "7-11"
    if n <= 14:
        return "12-14"
    if n <= 23:
        return "15-23"
    return "24+"


def money(x):
    return f"${x:,.0f}"


def load(data_dir: Path):
    orders = pd.read_csv(data_dir / "commerce7_orders.csv", dtype=str, keep_default_na=False)
    items = pd.read_csv(data_dir / "commerce7_order_items.csv", dtype=str, keep_default_na=False)

    for c in ("total", "subTotal", "shippingTotal", "tax", "discountTotal"):
        orders[c] = pd.to_numeric(orders[c], errors="coerce").fillna(0)
    orders["orderPaidDate"] = pd.to_datetime(orders["orderPaidDate"], errors="coerce", utc=True)

    items["quantity"] = pd.to_numeric(items["quantity"], errors="coerce").fillna(0)
    items["priceTotal"] = pd.to_numeric(items["priceTotal"], errors="coerce").fillna(0)

    wine = items[
        ~items["productTitle"].str.upper().str.contains(NON_WINE, na=False)
        & (items["paymentStatus"] == "Paid")
    ]
    per_order = wine.groupby("orderId").agg(
        bottles=("quantity", "sum"), wine_rev=("priceTotal", "sum")
    )

    o = orders[orders["paymentStatus"] == "Paid"].merge(
        per_order, left_on="id", right_index=True, how="left"
    )
    o[["bottles", "wine_rev"]] = o[["bottles", "wine_rev"]].fillna(0)
    o = o[o["shipToStateCode"].str.strip() != ""].copy()   # shipped only
    o["bucket"] = o["bottles"].map(bucket)
    o["summer"] = o["orderPaidDate"].dt.month.isin(SUMMER_MONTHS)
    o["season"] = np.where(o["summer"], "summer", "winter")
    return o


def build_rate_card(o, verbose=True):
    """Modal full-freight charge by (season, bucket, state), with fallbacks."""
    ff = o[o["shippingTotal"] >= FULL_FREIGHT_MIN]
    if ff.empty:
        sys.exit("No full-freight shipments found — cannot recover a rate card.")

    def mode(s):
        return s.value_counts().idxmax()

    by_state = ff.groupby(["season", "bucket", "shipToStateCode"])["shippingTotal"].agg(
        rate=mode, n="size"
    ).reset_index()
    by_state = by_state[by_state["n"] >= 4]
    state_map = {(r.season, r.bucket, r.shipToStateCode): r.rate for _, r in by_state.iterrows()}

    by_bucket = ff.groupby(["season", "bucket"])["shippingTotal"].agg(
        rate=mode, n="size"
    ).reset_index()
    bucket_map = {(r.season, r.bucket): r.rate for _, r in by_bucket.iterrows()}
    season_map = ff.groupby("season")["shippingTotal"].agg(mode).to_dict()
    overall = mode(ff["shippingTotal"])

    if verbose:
        print(f"  full-freight shipments used: {len(ff):,}")
        print("  recovered standard rates by bottle count:")
        print(f"    {'bucket':<8}{'winter':>10}{'summer':>10}{'n(win)':>9}{'n(sum)':>9}")
        for b in ["1-3", "4-6", "7-11", "12-14", "15-23", "24+"]:
            w = bucket_map.get(("winter", b))
            s = bucket_map.get(("summer", b))
            nw = int(by_bucket[(by_bucket.season == "winter") & (by_bucket.bucket == b)]["n"].sum())
            ns = int(by_bucket[(by_bucket.season == "summer") & (by_bucket.bucket == b)]["n"].sum())
            print(f"    {b:<8}{money(w) if w else '-':>10}{money(s) if s else '-':>10}"
                  f"{nw:>9,}{ns:>9,}")

    def rate_for(season, b, state):
        return (state_map.get((season, b, state))
                or bucket_map.get((season, b))
                or season_map.get(season, overall))

    return rate_for


def rate_stability_check(o):
    """
    The rate card is pooled across both years, which is only valid if rates did
    not move. Sub-12-bottle summer orders were never touched by the case promo,
    so they are a clean like-for-like probe of whether the card drifted.
    """
    sub = o[(o["shippingTotal"] >= FULL_FREIGHT_MIN) & o["summer"]
            & (o["bottles"] > 0) & (o["bottles"] < 12)]
    out = {}
    for yr in (2025, 2026):
        s = sub[sub["orderPaidDate"].dt.year == yr]["shippingTotal"]
        out[yr] = {"median": float(s.median()) if len(s) else float("nan"),
                   "mean": float(s.mean()) if len(s) else float("nan"), "n": int(len(s))}
    return out


def analyse(win, rate_for, label):
    w = win.copy()
    w["expected"] = [rate_for(se, b, st) for se, b, st
                     in zip(w["season"], w["bucket"], w["shipToStateCode"])]
    w["subsidy"] = (w["expected"] - w["shippingTotal"]).clip(lower=0)

    def band(r):
        if r["shippingTotal"] <= 0.005:
            return "free ($0)"
        if r["shippingTotal"] <= 1.50:
            return "$1 promo"
        if r["subsidy"] > 0.50:
            return "partial discount"
        return "full freight"

    w["band"] = w.apply(band, axis=1)
    return w


def summarise(w, label):
    exp, col, sub = w["expected"].sum(), w["shippingTotal"].sum(), w["subsidy"].sum()
    print(f"\n  {label}")
    print(f"    shipped orders:            {len(w):,}")
    print(f"    bottles shipped:           {w['bottles'].sum():,.0f}")
    print(f"    wine revenue:              {money(w['wine_rev'].sum())}")
    print(f"    shipping at standard rates:{money(exp):>14}")
    print(f"    shipping actually collected:{money(col):>13}")
    print(f"    TOTAL SUBSIDY:             {money(sub):>14}")
    print(f"    cost recovery:             {col/exp*100 if exp else 0:.1f}%")
    print(f"    subsidy per shipped order: {money(sub/len(w)) if len(w) else '$0'}")
    print(f"    subsidy per bottle:        ${sub/w['bottles'].sum() if w['bottles'].sum() else 0:,.2f}")
    print("    by band:")
    print(f"      {'band':<20}{'orders':>8}{'expected':>12}{'collected':>12}{'subsidy':>12}")
    for b, g in w.groupby("band"):
        print(f"      {b:<20}{len(g):>8,}{money(g['expected'].sum()):>12}"
              f"{money(g['shippingTotal'].sum()):>12}{money(g['subsidy'].sum()):>12}")
    print("    by channel:")
    print(f"      {'channel':<20}{'orders':>8}{'subsidy':>12}{'recovery':>10}")
    for c, g in w.groupby("channel"):
        e, cl = g["expected"].sum(), g["shippingTotal"].sum()
        print(f"      {c:<20}{len(g):>8,}{money(g['subsidy'].sum()):>12}"
              f"{cl/e*100 if e else 0:>9.1f}%")
    return {"orders": len(w), "bottles": float(w["bottles"].sum()),
            "wine_revenue": float(w["wine_rev"].sum()), "expected": float(exp),
            "collected": float(col), "subsidy": float(sub),
            "recovery_pct": float(col / exp * 100) if exp else 0.0,
            "subsidy_per_order": float(sub / len(w)) if len(w) else 0.0,
            "by_band": {b: {"orders": len(g), "expected": float(g["expected"].sum()),
                            "collected": float(g["shippingTotal"].sum()),
                            "subsidy": float(g["subsidy"].sum())}
                        for b, g in w.groupby("band")},
            "by_channel": {c: {"orders": len(g), "subsidy": float(g["subsidy"].sum())}
                           for c, g in w.groupby("channel")}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="./_data")
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-08-16")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    o = load(Path(a.data_dir))
    S = pd.Timestamp(a.start, tz="UTC")
    E = pd.Timestamp(a.end, tz="UTC")
    S_LY, E_LY = S - pd.DateOffset(years=1), E - pd.DateOffset(years=1)

    print("=" * 78)
    print(f"TOTAL DTC SHIPPING SUBSIDY  |  {S:%b %d} - {E:%b %d}, 2026 vs 2025")
    print("=" * 78)

    print("\n[1] RECOVERED COMMERCE7 RATE CARD")
    rate_for = build_rate_card(o)

    print("\n[2] RATE STABILITY CHECK (sub-12-bottle summer orders, never promoted)")
    st = rate_stability_check(o)
    for yr, v in st.items():
        print(f"    {yr}: median {money(v['median'])}  mean {money(v['mean'])}  n={v['n']:,}")
    d25, d26 = st[2025]["median"], st[2026]["median"]
    if d25 and not np.isnan(d25) and not np.isnan(d26):
        drift = (d26 - d25) / d25 * 100
        print(f"    drift: {drift:+.1f}%  ->  "
              f"{'rates stable, pooled card is valid' if abs(drift) < 10 else 'RATES MOVED — treat YoY subsidy comparison with caution'}")

    now = analyse(o[(o["orderPaidDate"] >= S) & (o["orderPaidDate"] < E)], rate_for, "2026")
    ly = analyse(o[(o["orderPaidDate"] >= S_LY) & (o["orderPaidDate"] < E_LY)], rate_for, "2025")

    print("\n[3] PERIOD COMPARISON")
    n = summarise(now, f"Jun 1 - Aug 16, 2026")
    l = summarise(ly, f"Jun 1 - Aug 16, 2025")

    print("\n[4] YEAR OVER YEAR")
    print(f"    {'metric':<28}{'2025':>14}{'2026':>14}{'change':>14}")
    rows = [("shipped orders", l["orders"], n["orders"], "n"),
            ("shipping at standard rates", l["expected"], n["expected"], "$"),
            ("shipping collected", l["collected"], n["collected"], "$"),
            ("total subsidy", l["subsidy"], n["subsidy"], "$"),
            ("subsidy per order", l["subsidy_per_order"], n["subsidy_per_order"], "$"),
            ("cost recovery %", l["recovery_pct"], n["recovery_pct"], "%")]
    for name, a25, a26, kind in rows:
        if kind == "$":
            f = lambda v: money(v)
        elif kind == "%":
            f = lambda v: f"{v:.1f}%"
        else:
            f = lambda v: f"{v:,.0f}"
        delta = a26 - a25
        pct = f" ({delta/a25*100:+.0f}%)" if a25 else ""
        print(f"    {name:<28}{f(a25):>14}{f(a26):>14}{f(delta)+pct:>14}")

    extra = n["subsidy"] - l["subsidy"]
    print(f"\n    Incremental subsidy vs last year: {money(extra)}")
    if l["subsidy"]:
        print(f"    That is {n['subsidy']/l['subsidy']:.2f}x last year's shipping subsidy.")

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"window_2026": {"start": a.start, "end": a.end}, "rate_stability": st,
             "y2026": n, "y2025": l, "incremental_subsidy": float(extra)},
            indent=2, default=lambda v: v.item()))
        print(f"\n[written] {a.json}")


if __name__ == "__main__":
    main()
