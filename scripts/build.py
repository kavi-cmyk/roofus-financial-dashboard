#!/usr/bin/env python3
"""Combine the synced Shopify + Meta data into dist/index.html.

Inputs (all under data/):
  shopify_sales_daily.json   ShopifyQL daily sales (orders, gross, discounts, returns, shipping ...)
  raw/orders.jsonl           one order per line with its payment transactions and fees
  shopify_payouts.json       optional; real Shopify Payments payouts (needs read_shopify_payments)
  meta_daily.json            Meta ad spend per day
  meta_campaigns.json        Meta spend per campaign
  config.json                start date, OPEX and shipping-label inputs

Output: dist/index.html (dashboard/template.html with the data inlined).
"""
import json
import datetime as dt
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STORE_TZ = ZoneInfo("America/Los_Angeles")

COLLECT_KINDS = {"SALE", "CAPTURE"}


def load(name, default=None):
    p = DATA / name
    if not p.exists():
        return default
    return json.loads(p.read_text())


def local_date(iso):
    ts = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return ts.astimezone(STORE_TZ).date().isoformat()


def order_rollup(start):
    """Per-day cash movement from order transactions (by transaction date, store time)."""
    days = defaultdict(lambda: defaultdict(float))
    stats = {"orders": 0, "cancelled": 0, "gateways": defaultdict(float)}
    path = DATA / "raw" / "orders.jsonl"
    if not path.exists():
        return {}, stats
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        stats["orders"] += 1
        stats["cancelled"] += 1 if o.get("cancelled") else 0
        for t in o.get("txns", []):
            if t.get("status") != "SUCCESS":
                continue
            day = local_date(t.get("processedAt") or o["createdAt"])
            if day < start:
                continue
            gw = t.get("gateway") or "unknown"
            amt = float(t.get("amount") or 0)
            fee = float(t.get("fee") or 0)
            d = days[day]
            if t["kind"] in COLLECT_KINDS:
                stats["gateways"][gw] += amt
                if gw == "shopify_payments":
                    d["collected"] += amt
                else:
                    d["collected_other"] += amt
            elif t["kind"] == "REFUND":
                if gw == "shopify_payments":
                    d["refunds_paid"] += amt
                else:
                    d["refunds_other"] += amt
            d["fees"] += fee
    stats["gateways"] = {k: round(v, 2) for k, v in stats["gateways"].items()}
    return days, stats


def main():
    cfg = load("config.json")
    start = cfg["start_date"]
    sales = load("shopify_sales_daily.json")
    meta = load("meta_daily.json")
    camps = load("meta_campaigns.json", {"campaigns": []})
    payouts = load("shopify_payouts.json")
    cash, stats = order_rollup(start)

    spend = {r["date"]: r["spend"] for r in meta["daily"]}
    days = sorted({r["date"] for r in sales["daily"]} | set(spend) | set(cash))
    by_sales = {r["date"]: r for r in sales["daily"]}

    daily = []
    for day in days:
        if day < start:
            continue
        s = by_sales.get(day, {})
        c = cash.get(day, {})
        daily.append({
            "date": day,
            "orders": s.get("orders", 0),
            "gross": s.get("gross_sales", 0.0),
            "discounts": -s.get("discounts", 0.0),
            "returns": -s.get("returns", 0.0),
            "net": s.get("net_sales", 0.0),
            "shipping": s.get("shipping_charges", 0.0),
            "taxes": s.get("taxes", 0.0),
            "total": s.get("total_sales", 0.0),
            "collected": round(c.get("collected", 0.0), 2),
            "collected_other": round(c.get("collected_other", 0.0), 2),
            "refunds_paid": round(c.get("refunds_paid", 0.0), 2),
            "refunds_other": round(c.get("refunds_other", 0.0), 2),
            "fees": round(c.get("fees", 0.0), 2),
            "spend": spend.get(day, 0.0),
        })

    bundle = {
        "store": cfg["store"],
        "start": start,
        "end": daily[-1]["date"],
        "generated": dt.datetime.now(STORE_TZ).strftime("%Y-%m-%d %H:%M %Z"),
        "config": cfg,
        "daily": daily,
        "campaigns": camps["campaigns"],
        "payouts": payouts["payouts"] if payouts else None,
        "order_stats": stats,
        "meta_account_totals": meta.get("account_totals"),
    }

    template = (ROOT / "dashboard" / "template.html").read_text()
    out = template.replace("/*__DATA__*/null", json.dumps(bundle, separators=(",", ":")))
    (ROOT / "dist").mkdir(exist_ok=True)
    (ROOT / "dist" / "index.html").write_text(out)

    tot = lambda k: round(sum(d[k] for d in daily), 2)
    print(f"Built dist/index.html  {start} → {bundle['end']}  ({len(daily)} days)")
    for k in ("orders", "gross", "discounts", "returns", "net", "shipping", "collected",
              "collected_other", "refunds_paid", "fees", "spend"):
        print(f"  {k:16} {tot(k):>12,.2f}")


if __name__ == "__main__":
    main()
