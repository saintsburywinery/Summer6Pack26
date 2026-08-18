#!/usr/bin/env python3
"""
Magnum campaign analysis — featured units, attach revenue, basket depth.

Answers:
  1. How many featured magnums sold.
  2. How much OTHER wine revenue rode along in those same orders (attach).
  3. What share of those orders took 3+ magnums.

The featured set is DISCOVERED, not hard-coded, and every magnum that moved in
the window is listed so the selection can be audited. A campaign report that
silently locked onto the wrong bottles would be indistinguishable from a
correct one.

The featured set is a group, not a single SKU: a vintage-themed campaign spans
every magnum of that vintage. Pass --vintage '' to fall back to all magnums.

  python3 magnum_campaign.py --data-dir ./_data --start 2026-08-14 --vintage 2014
"""
import argparse
import json
import sys
from datetime import timezone, timedelta
from pathlib import Path

import pandas as pd

NON_WINE = "TASTING|TASTE|GIFT CARD|MEMBERSHIP|SHIPPING|EVENT|TICKET|MERCH"

# Fixed offset rather than a named zone: tzdata is not guaranteed to be present,
# and a window inside one DST period needs no rule. August is PDT = UTC-7.


def money(x):
    return f"${x:,.0f}"


def load(data_dir: Path):
    items = pd.read_csv(data_dir / "commerce7_order_items.csv", dtype=str, keep_default_na=False)
    orders = pd.read_csv(data_dir / "commerce7_orders.csv", dtype=str, keep_default_na=False)

    for c in ("quantity", "price", "priceTotal", "originalTotal"):
        items[c] = pd.to_numeric(items[c], errors="coerce").fillna(0)
    items["orderPaidDate"] = pd.to_datetime(items["orderPaidDate"], errors="coerce", utc=True)
    for c in ("total", "shippingTotal"):
        orders[c] = pd.to_numeric(orders[c], errors="coerce").fillna(0)

    items = items[items["paymentStatus"] == "Paid"].copy()
    items["is_wine"] = ~items["productTitle"].str.upper().str.contains(NON_WINE, na=False)

    # Magnum detection. The format field is frequently blank, and titles carry
    # the size in two conventions — "... Magnum (1.5L)" and plain "... 1.5L".
    # Matching only the word MAGNUM silently drops the second form, which is
    # most of them.
    fmt = items["format"].str.upper()
    title = items["productTitle"].str.upper()
    items["is_magnum"] = (
        fmt.str.contains("1.5", regex=False, na=False)
        | fmt.str.contains("MAG", na=False)
        | title.str.contains("MAGNUM", na=False)
        | title.str.contains(r"1\.5\s*L", regex=True, na=False)
    )

    # The vintage column is often blank; titles reliably lead with the year.
    yr = items["productTitle"].str.extract(r"\b((?:19|20)\d{2})\b")[0]
    items["vintage_eff"] = yr.fillna(items["vintage"].astype(str).str.strip())
    return items, orders


def product_table(df, label, limit=None):
    t = df.groupby(["sku", "productTitle"]).agg(
        units=("quantity", "sum"), revenue=("priceTotal", "sum"),
        orders=("orderId", "nunique"),
    ).reset_index().sort_values("units", ascending=False)
    shown = t if limit is None else t.head(limit)
    print(f"  {label} ({len(t)} products):")
    print(f"    {'sku':<12}{'units':>7}{'orders':>8}{'revenue':>11}  title")
    for _, r in shown.iterrows():
        print(f"    {str(r['sku'])[:11]:<12}{r['units']:>7,.0f}{r['orders']:>8,}"
              f"{money(r['revenue']):>11}  {r['productTitle'][:46]}")
    if limit and len(t) > limit:
        print(f"    ... and {len(t)-limit} more")
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="./_data")
    ap.add_argument("--start", default="2026-08-14")
    ap.add_argument("--end", default=None)
    ap.add_argument("--vintage", default="2014",
                    help="featured vintage; blank = every magnum")
    ap.add_argument("--utc-offset", type=int, default=-7)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    LOCAL = timezone(timedelta(hours=a.utc_offset))
    items, orders = load(Path(a.data_dir))

    S = pd.Timestamp(a.start, tz=LOCAL).tz_convert("UTC")
    E = (pd.Timestamp(a.end, tz=LOCAL).tz_convert("UTC") if a.end
         else pd.Timestamp.now(tz="UTC"))

    print("=" * 78)
    print(f"MAGNUM CAMPAIGN  |  {S.tz_convert(LOCAL):%a %b %d %Y %H:%M} -> "
          f"{E.tz_convert(LOCAL):%a %b %d %H:%M} (UTC{a.utc_offset:+d})")
    print("=" * 78)

    win = items[(items["orderPaidDate"] >= S) & (items["orderPaidDate"] < E)]
    if win.empty:
        sys.exit("No paid line items in the window.")
    wine = win[win["is_wine"]]
    print(f"\n  window: {len(win):,} paid line items / {win['orderId'].nunique():,} orders")
    print(f"  wine bottles sold (all formats): {wine['quantity'].sum():,.0f}")
    print(f"  total wine revenue:              {money(wine['priceTotal'].sum())}")

    print("\n[1] EVERY MAGNUM SOLD IN THE WINDOW")
    mags = wine[wine["is_magnum"]]
    if mags.empty:
        sys.exit("No magnum-format wine sold in the window.")
    mag_tbl = product_table(mags, "magnums")
    print(f"    TOTAL magnum units: {mags['quantity'].sum():,.0f}   "
          f"revenue {money(mags['priceTotal'].sum())}   "
          f"orders {mags['orderId'].nunique():,}")

    print("\n  magnum units by vintage:")
    for v, g in mags.groupby("vintage_eff"):
        print(f"    {str(v) or '(unknown)':<10}{g['quantity'].sum():>7,.0f} units"
              f"{money(g['priceTotal'].sum()):>11}   {g['orderId'].nunique():>4} orders")

    print("\n[2] FEATURED SET")
    if a.vintage:
        feat = mags[mags["vintage_eff"] == a.vintage]
        label = f"{a.vintage} magnums"
        if feat.empty:
            print(f"  [WARN] no {a.vintage} magnums sold — falling back to ALL magnums.")
            feat, label = mags, "all magnums"
    else:
        feat, label = mags, "all magnums"
    feat_skus = sorted(feat["sku"].unique())
    print(f"  featured = {label}  ({len(feat_skus)} sku(s): {', '.join(map(str, feat_skus))})")

    feat_orders = set(feat["orderId"])
    units = feat["quantity"].sum()
    revenue = feat["priceTotal"].sum()
    print(f"\n  FEATURED MAGNUMS SOLD:     {units:,.0f}")
    print(f"  orders containing them:    {len(feat_orders):,}")
    print(f"  featured revenue:          {money(revenue)}")
    print(f"  avg magnums per order:     {units/len(feat_orders):.2f}")
    print(f"  avg selling price:         {money(revenue/units) if units else '$0'}")
    orig = feat["originalTotal"].sum()
    print(f"  effective discount:        {(1-revenue/orig)*100 if orig else 0:.1f}%")

    print("\n[3] ATTACH — OTHER WINE REVENUE IN THE SAME ORDERS")
    in_orders = wine[wine["orderId"].isin(feat_orders)]
    other = in_orders[~in_orders["sku"].isin(feat_skus)]
    attach_rev = other["priceTotal"].sum()
    total_wine = in_orders["priceTotal"].sum()
    with_attach = other["orderId"].nunique()
    print(f"  ADDITIONAL WINE REVENUE:   {money(attach_rev)}")
    print(f"  additional bottles:        {other['quantity'].sum():,.0f}")
    print(f"  orders with an attach:     {with_attach:,} of {len(feat_orders):,} "
          f"({with_attach/len(feat_orders)*100:.1f}%)")
    print(f"  attach revenue per order:  {money(attach_rev/len(feat_orders))}")
    print(f"  total wine rev, these orders: {money(total_wine)}")
    print(f"  attach share of that:      {attach_rev/max(total_wine,1)*100:.1f}%")
    if not other.empty:
        print()
        product_table(other, "attached products", limit=12)

    print("\n[4] BASKET DEPTH — 3+ MAGNUMS")
    per_feat = feat.groupby("orderId")["quantity"].sum()
    per_allmag = (wine[wine["is_magnum"] & wine["orderId"].isin(feat_orders)]
                  .groupby("orderId")["quantity"].sum())
    n = len(per_feat)
    res = {}
    for lbl, s in (("featured magnums", per_feat), ("any magnum", per_allmag)):
        k = int((s >= 3).sum())
        res[lbl] = k / n * 100
        print(f"  {lbl:<20} 3+ in {k:,} of {n:,} orders  ({k/n*100:.1f}%)")
    print("\n  distribution (featured magnum qty per order):")
    for q, c in per_feat.value_counts().sort_index().items():
        print(f"    {int(q):>3}: {c:>5,} orders  ({c/n*100:>5.1f}%)")

    print("\n[5] CHANNEL + DAILY")
    o_slim = orders[["id", "channel", "total", "discountCodes"]].rename(columns={"id": "orderId"})
    fo = pd.DataFrame({"orderId": sorted(feat_orders)}).merge(o_slim, on="orderId", how="left")
    fo = fo.merge(feat.groupby("orderId")["quantity"].sum().rename("mags"),
                  on="orderId", how="left")
    print(f"    {'channel':<12}{'orders':>8}{'magnums':>9}{'order total':>13}")
    for ch, g in fo.groupby("channel"):
        print(f"    {str(ch):<12}{len(g):>8,}{g['mags'].sum():>9,.0f}{money(g['total'].sum()):>13}")
    codes = fo["discountCodes"].value_counts().head(5)
    if len(codes):
        print("  discount codes on featured orders:")
        for c, k in codes.items():
            print(f"    {(str(c) or '(none)')[:52]:<52}{k:>5}")
    print("\n  by day (local):")
    fd = feat.copy()
    fd["day"] = fd["orderPaidDate"].dt.tz_convert(LOCAL).dt.date
    for d, g in fd.groupby("day"):
        print(f"    {d}  {g['quantity'].sum():>5,.0f} magnums  "
              f"{g['orderId'].nunique():>4,} orders  {money(g['priceTotal'].sum()):>10}")

    print("\n[6] TOP SELLERS IN WINDOW (all formats — campaign context)")
    product_table(wine, "all wine", limit=15)

    if a.json:
        Path(a.json).write_text(json.dumps({
            "window": {"start": str(S), "end": str(E)},
            "featured": {"label": label, "skus": list(map(str, feat_skus)),
                         "units": float(units), "revenue": float(revenue),
                         "orders": len(feat_orders)},
            "attach": {"revenue": float(attach_rev),
                       "bottles": float(other["quantity"].sum()),
                       "orders_with_attach": int(with_attach),
                       "total_wine_in_orders": float(total_wine)},
            "depth_pct_3plus": res,
            "all_magnums": mag_tbl.to_dict("records"),
        }, indent=2, default=str))
        print(f"\n[written] {a.json}")


if __name__ == "__main__":
    main()
