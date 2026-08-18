#!/usr/bin/env python3
"""
Best send time for a DTC campaign message — derived from when this winery's
customers actually place orders, not from generic email-marketing folklore.

Looks at three things:
  1. Hour-of-day distribution of self-serve DTC orders (Web + Inbound). Club is
     excluded because those are auto-billed and say nothing about when a person
     chooses to buy; POS is excluded because it is in-person foot traffic.
  2. The same distribution for one weekday, since a Wednesday send should be
     timed against Wednesday behaviour.
  3. Where the customer base actually is. A single Pacific send time lands at
     very different local times across the list, which drives both reach and
     SMS quiet-hours compliance.

  python3 send_time_analysis.py --data-dir ./_data --weekday 2
"""
import argparse
import json
from datetime import timezone, timedelta
from pathlib import Path

import pandas as pd

NON_WINE = "TASTING|TASTE|GIFT CARD|MEMBERSHIP|SHIPPING|EVENT|TICKET|MERCH"

# August: every US zone is on DST except Arizona and Hawaii.
STATE_OFFSET = {
    **{s: -4 for s in "CT DE FL GA IN KY ME MD MA MI NH NJ NY NC OH PA RI SC VT VA WV DC".split()},
    **{s: -5 for s in "AL AR IL IA KS LA MN MS MO NE ND OK SD TN TX WI".split()},
    **{s: -6 for s in "CO ID MT NM UT WY".split()},
    "AZ": -7,
    **{s: -7 for s in "CA NV OR WA".split()},
    "AK": -8, "HI": -10,
}
ZONE_NAME = {-4: "Eastern", -5: "Central", -6: "Mountain", -7: "Pacific/AZ",
             -8: "Alaska", -10: "Hawaii"}

# TCPA restricts marketing SMS to 8am-9pm in the RECIPIENT's local time.
QUIET_START, QUIET_END = 8, 21


def money(x):
    return f"${x:,.0f}"


def bar(frac, width=34):
    return "#" * int(round(frac * width))


def load(data_dir: Path, offset):
    LOCAL = timezone(timedelta(hours=offset))
    orders = pd.read_csv(data_dir / "commerce7_orders.csv", dtype=str, keep_default_na=False)
    items = pd.read_csv(data_dir / "commerce7_order_items.csv", dtype=str, keep_default_na=False)

    orders["orderPaidDate"] = pd.to_datetime(orders["orderPaidDate"], errors="coerce", utc=True)
    orders["total"] = pd.to_numeric(orders["total"], errors="coerce").fillna(0)
    orders = orders[(orders["paymentStatus"] == "Paid") & orders["orderPaidDate"].notna()].copy()

    items["priceTotal"] = pd.to_numeric(items["priceTotal"], errors="coerce").fillna(0)
    wine = items[(items["paymentStatus"] == "Paid")
                 & ~items["productTitle"].str.upper().str.contains(NON_WINE, na=False)]
    rev = wine.groupby("orderId")["priceTotal"].sum().rename("wine_rev")
    orders = orders.merge(rev, left_on="id", right_index=True, how="left")
    orders["wine_rev"] = orders["wine_rev"].fillna(0)

    local = orders["orderPaidDate"].dt.tz_convert(LOCAL)
    orders["hour"] = local.dt.hour
    orders["weekday"] = local.dt.weekday          # Mon=0
    orders["date"] = local.dt.date
    return orders


def hour_profile(df, label, top_n=4):
    if df.empty:
        print(f"  {label}: no orders")
        return None
    g = df.groupby("hour").agg(orders=("id", "count"), revenue=("wine_rev", "sum"))
    g = g.reindex(range(24), fill_value=0)
    tot_o, tot_r = g["orders"].sum(), g["revenue"].sum()
    print(f"\n  {label}  (n={tot_o:,} orders, {money(tot_r)})")
    print(f"    {'hr':>3}{'orders':>8}{'share':>8}{'revenue':>11}{'rev%':>7}  profile")
    for h, r in g.iterrows():
        so = r["orders"] / tot_o if tot_o else 0
        sr = r["revenue"] / tot_r if tot_r else 0
        print(f"    {h:>3}{int(r['orders']):>8,}{so*100:>7.1f}%{money(r['revenue']):>11}"
              f"{sr*100:>6.1f}%  {bar(so)}")
    best = g.sort_values("orders", ascending=False).head(top_n)
    print(f"    peak hours by order count: "
          f"{', '.join(f'{int(h)}:00 ({int(v)})' for h, v in best['orders'].items())}")
    return g


def rolling_window(g, width=3):
    """Best contiguous N-hour block by order share — a send window, not a point."""
    if g is None:
        return None
    o = g["orders"].to_numpy()
    tot = o.sum()
    best_h, best_v = None, -1
    for h in range(24 - width + 1):
        v = o[h:h + width].sum()
        if v > best_v:
            best_h, best_v = h, v
    return best_h, best_h + width, best_v / tot * 100 if tot else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="./_data")
    ap.add_argument("--utc-offset", type=int, default=-7, help="winery local offset; -7 = PDT")
    ap.add_argument("--weekday", type=int, default=2, help="0=Mon .. 6=Sun; 2=Wednesday")
    ap.add_argument("--recent-days", type=int, default=90)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    o = load(Path(a.data_dir), a.utc_offset)
    wd_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][a.weekday]

    print("=" * 78)
    print(f"SEND-TIME ANALYSIS  |  target weekday: {wd_name}  |  clock: UTC{a.utc_offset:+d}")
    print("=" * 78)

    # Self-serve only: an SMS drives Web/Inbound, not club auto-bills or walk-ins.
    dtc = o[o["channel"].isin(["Web", "Inbound"])]
    print(f"\n  paid orders loaded: {len(o):,}   self-serve DTC (Web+Inbound): {len(dtc):,}")
    print(f"  date range: {o['date'].min()} -> {o['date'].max()}")
    by_ch = o.groupby("channel")["id"].count().sort_values(ascending=False)
    print("  channel mix: " + ", ".join(f"{c} {n:,}" for c, n in by_ch.items()))

    print("\n[1] WHEN SELF-SERVE ORDERS HAPPEN — ALL DAYS, FULL HISTORY")
    g_all = hour_profile(dtc, "Web + Inbound, all days")

    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=a.recent_days)
    recent = dtc[dtc["orderPaidDate"] >= cutoff]
    print(f"\n[2] LAST {a.recent_days} DAYS ONLY (recency check — has behaviour shifted?)")
    g_recent = hour_profile(recent, f"Web + Inbound, last {a.recent_days}d")

    print(f"\n[3] {wd_name.upper()}S ONLY")
    g_wd = hour_profile(dtc[dtc["weekday"] == a.weekday], f"Web + Inbound, {wd_name}s")

    print("\n[4] BEST CONTIGUOUS 3-HOUR WINDOW")
    out = {}
    for lbl, g in (("all days", g_all), (f"last {a.recent_days}d", g_recent), (wd_name, g_wd)):
        w = rolling_window(g)
        if w:
            s, e, pct = w
            out[lbl] = {"start": s, "end": e, "pct_of_orders": pct}
            print(f"    {lbl:<14} {s:02d}:00-{e:02d}:00 local  ->  {pct:.1f}% of orders")

    print("\n[5] WHERE THE LIST IS — REACH AND SMS QUIET HOURS")
    st = dtc[dtc["shipToStateCode"].str.strip() != ""].copy()
    st["offset"] = st["shipToStateCode"].str.upper().map(STATE_OFFSET)
    known = st[st["offset"].notna()]
    zt = known.groupby("offset").agg(orders=("id", "count")).sort_values("orders", ascending=False)
    tot = zt["orders"].sum()
    print(f"    based on {tot:,} shipped self-serve orders with a mapped state")
    for off, r in zt.iterrows():
        print(f"    {ZONE_NAME.get(off, str(off)):<12} UTC{int(off):+d}  {int(r['orders']):>7,}"
              f"  {r['orders']/tot*100:>5.1f}%")
    unmapped = len(st) - len(known)
    if unmapped:
        print(f"    (unmapped/intl: {unmapped:,})")

    print(f"\n    a send at each winery-local hour, in recipients' local time:")
    print(f"    {'send':>6}  {'reach within 8am-9pm local':<30} breakdown")
    legal = {}
    for h in range(6, 21):
        ok = 0.0
        parts = []
        for off, r in zt.iterrows():
            lh = (h + (off - a.utc_offset)) % 24
            share = r["orders"] / tot
            if QUIET_START <= lh < QUIET_END:
                ok += share
            parts.append(f"{ZONE_NAME.get(off,'?')[:3]} {int(lh):02d}h")
        legal[h] = ok * 100
        flag = "" if ok > 0.999 else "  <-- some recipients outside quiet hours"
        print(f"    {h:02d}:00  {ok*100:>25.1f}%  {' | '.join(parts)}{flag}")

    if a.json:
        Path(a.json).write_text(json.dumps({
            "weekday": wd_name, "windows": out,
            "zone_share": {ZONE_NAME.get(k, str(k)): float(v / tot * 100)
                           for k, v in zt["orders"].items()},
            "legal_reach_by_hour": legal,
            "hour_orders_all": g_all["orders"].tolist() if g_all is not None else [],
            "hour_orders_weekday": g_wd["orders"].tolist() if g_wd is not None else [],
        }, indent=2, default=str))
        print(f"\n[written] {a.json}")


if __name__ == "__main__":
    main()
