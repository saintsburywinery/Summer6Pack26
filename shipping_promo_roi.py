#!/usr/bin/env python3
"""
$1-shipping-on-cases promotion — ROI analysis.

Answers three questions:
  1. Incremental DTC sales attributable to the promo (difference-in-differences
     against non-case orders, so overall business growth is netted out).
  2. Forgone shipping revenue (what those same orders would have been charged
     at the pre-promotion Commerce7 rates).
  3. Whether the economics improve once shipments default to Ground in Oct-May,
     using the observed seasonal air-vs-ground premium in historical rates.

Reads the dashboard CSVs. No API calls, no credentials.

  python3 shipping_promo_roi.py                          # defaults to ~/commerce7-dashboard
  python3 shipping_promo_roi.py --data-dir /path/to/csvs
  python3 shipping_promo_roi.py --promo-start 2026-06-01 --json out.json
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# A "case" for promo purposes. Commerce7 promo is 12+ bottles.
CASE_BOTTLES = 12
# An order is treated as having received the promo if shipping was charged at
# roughly a dollar. Widened slightly to absorb rounding / partial tax handling.
PROMO_SHIP_MIN, PROMO_SHIP_MAX = 0.01, 1.50

NON_WINE = "TASTING|TASTE|GIFT CARD|MEMBERSHIP|SHIPPING|EVENT|TICKET|MERCH"


# ----------------------------------------------------------------------------
# load
# ----------------------------------------------------------------------------
def load(data_dir: Path):
    orders = pd.read_csv(data_dir / "commerce7_orders.csv", dtype=str, keep_default_na=False)
    items = pd.read_csv(data_dir / "commerce7_order_items.csv", dtype=str, keep_default_na=False)

    for col in ("total", "subTotal", "shippingTotal", "tax", "discountTotal", "itemQty"):
        orders[col] = pd.to_numeric(orders[col], errors="coerce").fillna(0)
    orders["orderPaidDate"] = pd.to_datetime(orders["orderPaidDate"], errors="coerce", utc=True)

    items["quantity"] = pd.to_numeric(items["quantity"], errors="coerce").fillna(0)
    items["priceTotal"] = pd.to_numeric(items["priceTotal"], errors="coerce").fillna(0)
    items["originalTotal"] = pd.to_numeric(items["originalTotal"], errors="coerce").fillna(0)

    # Bottle count and wine revenue per order, wine line items only.
    wine = items[
        ~items["productTitle"].str.upper().str.contains(NON_WINE, na=False)
        & (items["paymentStatus"] == "Paid")
    ]
    per_order = wine.groupby("orderId").agg(
        bottles=("quantity", "sum"),
        wine_rev=("priceTotal", "sum"),
        wine_retail=("originalTotal", "sum"),
    )

    o = orders[orders["paymentStatus"] == "Paid"].merge(
        per_order, left_on="id", right_index=True, how="left"
    )
    o[["bottles", "wine_rev", "wine_retail"]] = o[["bottles", "wine_rev", "wine_retail"]].fillna(0)

    # Shipped orders only — a blank ship-to state is a tasting-room pickup.
    o["is_shipped"] = o["shipToStateCode"].str.strip() != ""
    o["is_case"] = o["bottles"] >= CASE_BOTTLES
    o["got_promo"] = (
        o["is_shipped"]
        & o["is_case"]
        & o["shippingTotal"].between(PROMO_SHIP_MIN, PROMO_SHIP_MAX)
    )
    o["month"] = o["orderPaidDate"].dt.tz_convert(None).dt.to_period("M")
    return o


def window(df, start, end):
    return df[(df["orderPaidDate"] >= start) & (df["orderPaidDate"] < end)]


def money(x):
    return f"${x:,.0f}"


# ----------------------------------------------------------------------------
# 1. counterfactual shipping rate table, built from what was ACTUALLY charged
#    before the promo rather than from a published rate card
# ----------------------------------------------------------------------------
def bottle_bucket(n):
    if n < 12:
        return "<12"
    if n < 15:
        return "12-14"
    if n < 24:
        return "15-23"
    return "24+"


def build_rate_table(pre, season_months=None, verbose=True):
    """
    Median shipping charged on pre-promo shipped case orders, by state x bucket.

    Season-matched: a summer promo has to be priced against what summer
    shipments actually cost (2-day air to protect the wine from heat), not
    against a trailing-year median that is half cheap winter Ground. Falls back
    to the full year if the season-matched sample is too thin to trust.
    """
    base = pre[pre["is_shipped"] & pre["is_case"] & (pre["shippingTotal"] > PROMO_SHIP_MAX)].copy()
    if base.empty:
        sys.exit("No pre-promotion case orders found — cannot build a counterfactual rate table.")

    if season_months:
        seasonal = base[base["orderPaidDate"].dt.month.isin(season_months)]
        if len(seasonal) >= 30:
            base = seasonal.copy()
            if verbose:
                print(f"  rate table season-matched to months {sorted(season_months)}")
        elif verbose:
            print(f"  season-matched sample too thin (n={len(seasonal)}) — using full trailing year")

    base["bucket"] = base["bottles"].map(bottle_bucket)

    by_state = (
        base.groupby(["shipToStateCode", "bucket"])["shippingTotal"]
        .agg(["median", "count"])
        .reset_index()
    )
    by_state = by_state[by_state["count"] >= 5]  # need a real sample to trust a state
    state_map = {
        (r.shipToStateCode, r.bucket): r["median"] for _, r in by_state.iterrows()
    }
    bucket_map = base.groupby("bucket")["shippingTotal"].median().to_dict()
    national = base["shippingTotal"].median()

    if verbose:
        print("  pre-promo case shipments sampled:", f"{len(base):,}")
        print("  national median case shipping:   ", money(national))
        for b, v in sorted(bucket_map.items()):
            n = (base["bucket"] == b).sum()
            print(f"    {b:>6} bottles: {money(v):>8}  (n={n:,})")

    def rate_for(state, bottles):
        b = bottle_bucket(bottles)
        return state_map.get((state, b), bucket_map.get(b, national))

    return rate_for, national


# ----------------------------------------------------------------------------
# 2. incrementality — difference-in-differences
# ----------------------------------------------------------------------------
def did(promo_now, promo_ly, label_a="case", label_b="non-case"):
    """
    Cases are the treated group; sub-12-bottle shipped orders are the control.
    Incremental = actual case revenue this year, minus what case revenue would
    have been had it grown at the same rate the untreated control group did.
    """
    def split(df):
        s = df[df["is_shipped"]]
        return s[s["is_case"]], s[~s["is_case"]]

    case_now, ctrl_now = split(promo_now)
    case_ly, ctrl_ly = split(promo_ly)

    def rev(d):
        return d["wine_rev"].sum()

    ctrl_growth = rev(ctrl_now) / rev(ctrl_ly) if rev(ctrl_ly) else np.nan
    counterfactual = rev(case_ly) * ctrl_growth
    incremental = rev(case_now) - counterfactual

    ord_growth = len(ctrl_now) / len(ctrl_ly) if len(ctrl_ly) else np.nan
    inc_orders = len(case_now) - len(case_ly) * ord_growth

    return {
        "case_rev_now": rev(case_now),
        "case_rev_ly": rev(case_ly),
        "ctrl_rev_now": rev(ctrl_now),
        "ctrl_rev_ly": rev(ctrl_ly),
        "ctrl_growth": ctrl_growth,
        "counterfactual_case_rev": counterfactual,
        "incremental_rev": incremental,
        "case_orders_now": len(case_now),
        "case_orders_ly": len(case_ly),
        "ctrl_order_growth": ord_growth,
        "incremental_orders": inc_orders,
        "case_aov_now": rev(case_now) / len(case_now) if len(case_now) else 0,
        "case_aov_ly": rev(case_ly) / len(case_ly) if len(case_ly) else 0,
        "case_bottles_now": case_now["bottles"].mean() if len(case_now) else 0,
        "case_bottles_ly": case_ly["bottles"].mean() if len(case_ly) else 0,
    }


# ----------------------------------------------------------------------------
# 3. seasonal air-vs-ground premium
# ----------------------------------------------------------------------------
def seasonal_premium(hist, rate_national):
    """
    Oct-May ships Ground; Jun-Sep needs 2-day/expedited to protect the wine from
    heat. Compare what was actually charged on case shipments in each season,
    pre-promotion, to size the premium the promo is currently absorbing.
    """
    base = hist[hist["is_shipped"] & hist["is_case"] & (hist["shippingTotal"] > PROMO_SHIP_MAX)].copy()
    base["mo"] = base["orderPaidDate"].dt.month
    base["season"] = np.where(base["mo"].isin([6, 7, 8, 9]), "Jun-Sep (air)", "Oct-May (ground)")

    out = base.groupby("season")["shippingTotal"].agg(["median", "mean", "count"]).to_dict("index")
    air = out.get("Jun-Sep (air)", {}).get("median", rate_national)
    ground = out.get("Oct-May (ground)", {}).get("median", rate_national)
    return out, air, ground


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(Path.home() / "commerce7-dashboard"))
    ap.add_argument("--promo-start", default="2026-06-01")
    ap.add_argument("--promo-end", default=None, help="default: today")
    ap.add_argument("--json", default=None, help="also write the full result set here")
    a = ap.parse_args()

    data_dir = Path(a.data_dir).expanduser()
    if not (data_dir / "commerce7_orders.csv").exists():
        sys.exit(f"No commerce7_orders.csv in {data_dir} — pass --data-dir")

    o = load(data_dir)

    PS = pd.Timestamp(a.promo_start, tz="UTC")
    PE = pd.Timestamp(a.promo_end, tz="UTC") if a.promo_end else pd.Timestamp.now(tz="UTC").normalize()
    LY_S, LY_E = PS - pd.DateOffset(years=1), PE - pd.DateOffset(years=1)
    days = (PE - PS).days

    print("=" * 78)
    print(f"$1 SHIPPING ON CASES — ROI  |  {PS:%Y-%m-%d} to {PE:%Y-%m-%d}  ({days} days)")
    print("=" * 78)

    promo_win = window(o, PS, PE)
    ly_win = window(o, LY_S, LY_E)
    pre = window(o, PS - pd.DateOffset(months=12), PS)

    # -- cohort sanity check -------------------------------------------------
    print("\n[1] PROMO COHORT")
    shipped = promo_win[promo_win["is_shipped"]]
    got = promo_win[promo_win["got_promo"]]
    cases = shipped[shipped["is_case"]]
    print(f"  shipped orders in window:        {len(shipped):,}")
    print(f"  of which 12+ bottles (cases):    {len(cases):,}  ({len(cases)/max(len(shipped),1)*100:.1f}%)")
    print(f"  of which shipped at ~$1:         {len(got):,}  ({len(got)/max(len(cases),1)*100:.1f}% of cases)")
    print(f"  promo case wine revenue:         {money(got['wine_rev'].sum())}")
    print(f"  promo bottles shipped:           {got['bottles'].sum():,.0f}")
    codes = got["discountCodes"].value_counts().head(5)
    if len(codes):
        print("  top discount codes on promo orders:")
        for c, n in codes.items():
            print(f"    {(c or '(none)')[:44]:<44} {n:,}")
    by_ch = got.groupby("channel").agg(n=("id", "count"), rev=("wine_rev", "sum"))
    print("  by channel:")
    for ch, r in by_ch.iterrows():
        print(f"    {ch:<10} {int(r['n']):>6,} orders   {money(r['rev']):>12}")

    # -- cost of the discount ------------------------------------------------
    print("\n[2] COST — FORGONE SHIPPING REVENUE")
    promo_months = sorted({d.month for d in pd.date_range(PS, PE, freq="D")})
    rate_for, national = build_rate_table(pre, season_months=promo_months)
    got = got.copy()
    got["would_have_charged"] = [
        rate_for(s, b) for s, b in zip(got["shipToStateCode"], got["bottles"])
    ]
    got["forgone"] = (got["would_have_charged"] - got["shippingTotal"]).clip(lower=0)
    forgone_total = got["forgone"].sum()
    print(f"  counterfactual shipping revenue: {money(got['would_have_charged'].sum())}")
    print(f"  actually collected:              {money(got['shippingTotal'].sum())}")
    print(f"  FORGONE SHIPPING REVENUE:        {money(forgone_total)}")
    print(f"  avg subsidy per case order:      {money(got['forgone'].mean() if len(got) else 0)}")

    # -- incremental sales ---------------------------------------------------
    print("\n[3] BENEFIT — INCREMENTAL DTC SALES (difference-in-differences)")
    d = did(promo_win, ly_win)
    print(f"  case revenue  this yr / last yr:  {money(d['case_rev_now'])} / {money(d['case_rev_ly'])}")
    print(f"  control (<12 btl) yr / last yr:   {money(d['ctrl_rev_now'])} / {money(d['ctrl_rev_ly'])}")
    print(f"  control growth factor:            {d['ctrl_growth']:.3f}x  <- the counterfactual trend")
    print(f"  expected case rev w/o promo:      {money(d['counterfactual_case_rev'])}")
    print(f"  INCREMENTAL CASE REVENUE:         {money(d['incremental_rev'])}")
    print(f"  incremental case orders:          {d['incremental_orders']:,.0f}")
    print(f"  case AOV   this yr / last yr:     {money(d['case_aov_now'])} / {money(d['case_aov_ly'])}")
    print(f"  bottles/case-order yr / last yr:  {d['case_bottles_now']:.1f} / {d['case_bottles_ly']:.1f}")

    # -- verdict -------------------------------------------------------------
    print("\n[4] VERDICT")
    gm = 0.70  # DTC gross margin on wine; override if you have a better number
    inc_gp = d["incremental_rev"] * gm
    net = inc_gp - forgone_total
    print(f"  incremental revenue:             {money(d['incremental_rev'])}")
    print(f"  x assumed {gm:.0%} DTC gross margin:   {money(inc_gp)}")
    print(f"  less forgone shipping revenue:   -{money(forgone_total)}")
    print(f"  NET CONTRIBUTION:                {money(net)}   {'WORTH IT' if net > 0 else 'NOT WORTH IT'}")
    if forgone_total:
        print(f"  break-even incremental revenue:  {money(forgone_total / gm)}")
        print(f"  return on subsidy spend:         {d['incremental_rev']/forgone_total:.2f}x revenue "
              f"/ {inc_gp/forgone_total:.2f}x gross profit")

    # -- seasonality ---------------------------------------------------------
    print("\n[5] OCT-MAY GROUND SHIFT")
    seas, air, ground = seasonal_premium(pre, national)
    for s, r in seas.items():
        print(f"  {s:<18} median {money(r['median']):>8}   mean {money(r['mean']):>8}   n={int(r['count']):,}")
    premium = air - ground
    print(f"  air-over-ground premium/case:    {money(premium)}")
    if len(got):
        proj = got["forgone"].mean() - premium
        print(f"  current avg subsidy per case:    {money(got['forgone'].mean())}")
        print(f"  projected Oct-May subsidy/case:  {money(proj)}  ({(1-proj/got['forgone'].mean())*100:.0f}% cheaper)")
        run_rate = len(got) / max(days, 1) * 30
        print(f"  at {run_rate:,.0f} promo cases/mo, monthly subsidy:")
        print(f"     now (air):     {money(run_rate * got['forgone'].mean())}")
        print(f"     Oct-May (gnd): {money(run_rate * proj)}")
        print(f"     monthly saving:{money(run_rate * premium)}")
        be_now = forgone_total / gm / max(d["incremental_rev"], 1)
        print(f"  break-even needs {be_now*100:.0f}% of current incremental lift to hold in winter")

    # -- monthly trend -------------------------------------------------------
    print("\n[6] MONTHLY TREND (shipped case orders)")
    m = o[o["is_shipped"] & o["is_case"]].copy()
    m = m[m["orderPaidDate"] >= PS - pd.DateOffset(months=14)]
    tbl = m.groupby("month").agg(
        orders=("id", "count"),
        rev=("wine_rev", "sum"),
        ship_collected=("shippingTotal", "sum"),
        avg_ship=("shippingTotal", "mean"),
        bottles=("bottles", "sum"),
    )
    print(f"  {'month':<9}{'orders':>8}{'wine rev':>13}{'avg ship':>10}{'bottles':>10}")
    for mo, r in tbl.iterrows():
        flag = " *" if pd.Timestamp(str(mo) + "-01", tz="UTC") >= PS else ""
        print(f"  {str(mo):<9}{int(r['orders']):>8,}{money(r['rev']):>13}"
              f"{money(r['avg_ship']):>10}{int(r['bottles']):>10,}{flag}")
    print("  (* = promotion active)")

    if a.json:
        payload = {
            "window": {"start": str(PS.date()), "end": str(PE.date()), "days": days},
            "cohort": {
                "shipped_orders": len(shipped), "case_orders": len(cases),
                "promo_orders": len(got), "promo_wine_revenue": float(got["wine_rev"].sum()),
                "promo_bottles": float(got["bottles"].sum()),
            },
            "cost": {
                "counterfactual_shipping": float(got["would_have_charged"].sum()),
                "collected": float(got["shippingTotal"].sum()),
                "forgone": float(forgone_total),
                "avg_subsidy_per_order": float(got["forgone"].mean()) if len(got) else 0,
            },
            "incrementality": {k: (float(v) if isinstance(v, (int, float, np.floating)) else v)
                               for k, v in d.items()},
            "verdict": {"gross_margin_assumed": gm, "incremental_gross_profit": float(inc_gp),
                        "net_contribution": float(net)},
            "seasonality": {"air_median": float(air), "ground_median": float(ground),
                            "premium": float(premium)},
        }
        Path(a.json).write_text(json.dumps(payload, indent=2))
        print(f"\n[written] {a.json}")


if __name__ == "__main__":
    main()
