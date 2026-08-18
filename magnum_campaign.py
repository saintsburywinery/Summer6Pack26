#!/usr/bin/env python3
"""
Magnum campaign analysis — featured-bottle units, attach revenue, basket depth.

Answers:
  1. How many featured magnums sold.
  2. How much OTHER wine revenue rode along in those same orders (attach).
  3. What share of those orders took 3+ magnums.

The featured SKU is discovered rather than hard-coded: the script lists every
magnum-format product that moved in the window so the match can be verified,
then scores candidates against --match / --vintage. A campaign analysis that
silently matched the wrong bottle would look identical to a real result, so
the candidate list is always reported.

  python3 magnum_campaign.py --data-dir ./_data --start 2026-08-14 \
      --match EARTHQUAKE --vintage 2014
"""
import argparse
import json
import sys
from pathlib import Path

from datetime import timezone, timedelta

import pandas as pd

NON_WINE = "TASTING|TASTE|GIFT CARD|MEMBERSHIP|SHIPPING|EVENT|TICKET|MERCH"

# Fixed offset rather than a named zone: the tzdata package is not guaranteed
# to be present, and a campaign inside a single DST period needs no rule.
# August is PDT = UTC-7. Override with --utc-offset if a window crosses a shift.


def money(x):
    return f"${x:,.0f}"


def load(data_dir: Path):
    items = pd.read_csv(data_dir / "commerce7_order_items.csv", dtype=str, keep_default_na=False)
    orders = pd.read_csv(data_dir / "commerce7_orders.csv", dtype=str, keep_default_na=False)

    for c in ("quantity", "price", "priceTotal", "originalTotal"):
        items[c] = pd.to_numeric(items[c], errors="coerce").fillna(0)
    items["orderPaidDate"] = pd.to_datetime(items["orderPaidDate"], errors="coerce", utc=True)
    orders["orderPaidDate"] = pd.to_datetime(orders["orderPaidDate"], errors="coerce", utc=True)
    for c in ("total", "shippingTotal"):
        orders[c] = pd.to_numeric(orders[c], errors="coerce").fillna(0)

    items = items[items["paymentStatus"] == "Paid"].copy()
    items["is_wine"] = ~items["productTitle"].str.upper().str.contains(NON_WINE, na=False)

    # Magnum detection: the format field is the primary signal, but it is not
    # always populated, so the product title is a fallback.
    fmt = items["format"].str.upper()
    title = items["productTitle"].str.upper()
    items["is_magnum"] = (
        fmt.str.contains("1.5", regex=False, na=False)
        | fmt.str.contains("MAG", na=False)
        | title.str.contains("MAGNUM", na=False)
    )
    return items, orders


def pick_featured(mags, match, vintage, verbose=True):
    """Rank magnum products in-window against the campaign description."""
    if mags.empty:
        return None, pd.DataFrame()

    cand = mags.groupby(["sku", "productTitle", "vintage"]).agg(
        units=("quantity", "sum"), revenue=("priceTotal", "sum"),
        orders=("orderId", "nunique"),
    ).reset_index().sort_values("units", ascending=False)

    if verbose:
        print("  magnum products sold in window:")
        print(f"    {'sku':<18}{'vintage':<9}{'units':>7}{'orders':>8}{'revenue':>11}  title")
        for _, r in cand.iterrows():
            print(f"    {r['sku'][:17]:<18}{str(r['vintage'])[:8]:<9}{r['units']:>7,.0f}"
                  f"{r['orders']:>8,}{money(r['revenue']):>11}  {r['productTitle'][:44]}")

    scored = cand.copy()
    blob = (scored["productTitle"].str.upper() + " " + scored["sku"].str.upper())
    scored["score"] = 0
    if match:
        scored["score"] += blob.str.contains(match.upper(), na=False).astype(int) * 10
    if vintage:
        scored["score"] += (
            (scored["vintage"].astype(str).str.strip() == str(vintage)).astype(int) * 5
            + blob.str.contains(str(vintage), na=False).astype(int) * 3
        )
    scored = scored.sort_values(["score", "units"], ascending=False)
    top = scored.iloc[0]
    if top["score"] == 0:
        if verbose:
            print(f"\n  [WARN] nothing matched '{match}' / vintage {vintage}. "
                  "Falling back to the best-selling magnum; verify against the list above.")
    return top, scored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="./_data")
    ap.add_argument("--start", default="2026-08-14", help="campaign start, Pacific")
    ap.add_argument("--end", default=None, help="exclusive, Pacific; default now")
    ap.add_argument("--match", default="EARTHQUAKE")
    ap.add_argument("--vintage", default="2014")
    ap.add_argument("--utc-offset", type=int, default=-7,
                    help="local offset in hours; -7 = PDT, -8 = PST")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    LOCAL = timezone(timedelta(hours=a.utc_offset))

    items, orders = load(Path(a.data_dir))

    S = pd.Timestamp(a.start, tz=LOCAL).tz_convert("UTC")
    E = (pd.Timestamp(a.end, tz=LOCAL).tz_convert("UTC") if a.end
         else pd.Timestamp.now(tz="UTC"))

    print("=" * 78)
    print(f"MAGNUM CAMPAIGN  |  {S.tz_convert(LOCAL):%a %b %d %Y %H:%M} -> {E.tz_convert(LOCAL):%a %b %d %H:%M} (UTC{a.utc_offset:+d})")
    print("=" * 78)

    win = items[(items["orderPaidDate"] >= S) & (items["orderPaidDate"] < E)]
    if win.empty:
        sys.exit("No paid line items in the campaign window — check --start/--end "
                 "and that the fetch covered these dates.")
    print(f"\n  paid line items in window: {len(win):,} "
          f"across {win['orderId'].nunique():,} orders")

    print("\n[1] FEATURED PRODUCT DISCOVERY")
    mags = win[win["is_magnum"] & win["is_wine"]]
    if mags.empty:
        sys.exit("No magnum-format wine sold in the window.")
    top, scored = pick_featured(mags, a.match, a.vintage)
    sku = top["sku"]
    print(f"\n  -> featured: {top['productTitle']}  (sku {sku}, vintage {top['vintage']})")

    feat = win[win["sku"] == sku]
    feat_orders = set(feat["orderId"])

    print("\n[2] FEATURED MAGNUM SALES")
    units = feat["quantity"].sum()
    revenue = feat["priceTotal"].sum()
    print(f"  magnums sold:              {units:,.0f}")
    print(f"  orders containing it:      {len(feat_orders):,}")
    print(f"  featured magnum revenue:   {money(revenue)}")
    print(f"  avg magnums per order:     {units/len(feat_orders):.2f}")
    print(f"  avg selling price/bottle:  {money(revenue/units) if units else '$0'}")
    disc = (1 - revenue / feat["originalTotal"].sum()) * 100 if feat["originalTotal"].sum() else 0
    print(f"  effective discount:        {disc:.1f}%")

    print("\n[3] ATTACH — OTHER WINE REVENUE IN THE SAME ORDERS")
    in_orders = win[win["orderId"].isin(feat_orders)]
    other_wine = in_orders[(in_orders["sku"] != sku) & in_orders["is_wine"]]
    attach_rev = other_wine["priceTotal"].sum()
    attach_units = other_wine["quantity"].sum()
    with_attach = other_wine["orderId"].nunique()
    print(f"  additional wine revenue:   {money(attach_rev)}")
    print(f"  additional bottles:        {attach_units:,.0f}")
    print(f"  orders with an attach:     {with_attach:,} of {len(feat_orders):,} "
          f"({with_attach/len(feat_orders)*100:.1f}%)")
    print(f"  attach revenue per order:  {money(attach_rev/len(feat_orders))}")
    print(f"  total wine rev in these orders: "
          f"{money(in_orders[in_orders['is_wine']]['priceTotal'].sum())}")
    print(f"  attach share of that total:{attach_rev/max(in_orders[in_orders['is_wine']]['priceTotal'].sum(),1)*100:>6.1f}%")

    if not other_wine.empty:
        print("\n  top attached products:")
        t = other_wine.groupby("productTitle").agg(
            units=("quantity", "sum"), rev=("priceTotal", "sum")
        ).sort_values("rev", ascending=False).head(10)
        for name, r in t.iterrows():
            print(f"    {name[:46]:<46}{r['units']:>7,.0f}{money(r['rev']):>11}")

    print("\n[4] BASKET DEPTH — 3+ MAGNUMS")
    per_order_feat = feat.groupby("orderId")["quantity"].sum()
    # All magnums, not just the featured bottle, since a mixed magnum basket
    # still reads as a magnum buyer.
    per_order_allmag = (win[win["is_magnum"] & win["is_wine"]
                            & win["orderId"].isin(feat_orders)]
                        .groupby("orderId")["quantity"].sum())
    n = len(per_order_feat)
    for label, s in (("featured magnum only", per_order_feat), ("any magnum", per_order_allmag)):
        three = int((s >= 3).sum())
        print(f"  {label:<22} 3+ in {three:,} of {n:,} orders  ({three/n*100:.1f}%)")
    print("\n  distribution (featured magnum qty per order):")
    dist = per_order_feat.value_counts().sort_index()
    for q, c in dist.items():
        print(f"    {int(q):>3} magnum(s): {c:>5,} orders  ({c/n*100:>5.1f}%)")

    print("\n[5] CHANNEL + DAILY")
    o_slim = orders[["id", "channel", "total", "discountCodes"]].rename(columns={"id": "orderId"})
    fo = pd.DataFrame({"orderId": list(feat_orders)}).merge(o_slim, on="orderId", how="left")
    fq = feat.groupby("orderId")["quantity"].sum().rename("mags")
    fo = fo.merge(fq, on="orderId", how="left")
    print(f"    {'channel':<12}{'orders':>8}{'magnums':>9}{'order total':>13}")
    for ch, g in fo.groupby("channel"):
        print(f"    {ch:<12}{len(g):>8,}{g['mags'].sum():>9,.0f}{money(g['total'].sum()):>13}")
    codes = fo["discountCodes"].value_counts().head(5)
    if len(codes):
        print("  top discount codes:")
        for c, k in codes.items():
            print(f"    {(c or '(none)')[:50]:<50}{k:>5}")
    print("\n  by day (local):")
    fd = feat.copy()
    fd["day"] = fd["orderPaidDate"].dt.tz_convert(LOCAL).dt.date
    for d, g in fd.groupby("day"):
        print(f"    {d}  {g['quantity'].sum():>5,.0f} magnums  "
              f"{g['orderId'].nunique():>4,} orders  {money(g['priceTotal'].sum()):>10}")

    if a.json:
        Path(a.json).write_text(json.dumps({
            "window": {"start": str(S), "end": str(E)},
            "featured": {"sku": sku, "title": top["productTitle"],
                         "vintage": str(top["vintage"]),
                         "units": float(units), "revenue": float(revenue),
                         "orders": len(feat_orders)},
            "attach": {"revenue": float(attach_rev), "bottles": float(attach_units),
                       "orders_with_attach": int(with_attach)},
            "depth": {"pct_3plus_featured": float((per_order_feat >= 3).mean() * 100),
                      "pct_3plus_any_magnum": float((per_order_allmag >= 3).mean() * 100)},
            "candidates": scored.to_dict("records"),
        }, indent=2, default=str))
        print(f"\n[written] {a.json}")


if __name__ == "__main__":
    main()
