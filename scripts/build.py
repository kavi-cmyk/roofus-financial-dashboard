#!/usr/bin/env python3
"""Combine the synced Shopify + Meta data into dist/index.html.

Inputs (all under data/):
  shopify_sales_daily.json   ShopifyQL daily sales (orders, gross, discounts, returns, shipping ...)
  raw/orders.jsonl           one order per line with its payment transactions and fees
  shopify_payouts.json       optional; real Shopify Payments payouts (needs read_shopify_payments)
  meta_daily.json            Meta ad spend per day
  meta_campaigns.json        Meta spend per campaign
  shopify_labels.json        Shopify Shipping label count and cost per day, by service and package
  raw/labels_by_order.json   optional; label count and cost per order, used to put label cost on the order date
  raw/units_by_product_daily.json  units and net sales per product per day, costed with config.cogs
  config.json                start date and OPEX inputs

Output: dist/index.html (dashboard/template.html with the data inlined).
"""
import json
import re
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


def order_dates():
    """order name -> (created date in store time, shipping charged)."""
    path = DATA / "raw" / "orders.jsonl"
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                o = json.loads(line)
                out[o["name"]] = (local_date(o["createdAt"]), float(o.get("shipping") or 0))
    return out


def labels_by_order_date(start, label_daily):
    """Label cost per day, keyed by the date the order was placed.

    Falls back to the label purchase date when there is no per-order export.
    Returns (per-day {labels, cost, orders}, info dict)."""
    per_order = load("raw/labels_by_order.json")
    if not per_order:
        return ({r["date"]: {"labels": r["labels"], "cost": r["cost"], "orders": 0} for r in label_daily},
                {"basis": "purchase_date"})
    dates = order_dates()
    orders = defaultdict(lambda: [0, 0.0])
    for r in per_order["rows"]:
        orders[r["order_name"]][0] += int(r["labels"])
        orders[r["order_name"]][1] += float(r["cost"])
    days = defaultdict(lambda: {"labels": 0, "cost": 0.0, "orders": 0})
    earlier = [0, 0, 0.0]      # orders placed before start: orders, labels, cost
    unmatched = [0, 0, 0.0]    # label rows whose order isn't in the export
    multi = 0
    for name, (n, cost) in orders.items():
        if n > 1:
            multi += 1
        if name not in dates:
            bucket = unmatched
        elif dates[name][0] < start:
            bucket = earlier
        else:
            d = days[dates[name][0]]
            d["labels"] += n
            d["cost"] += cost
            d["orders"] += 1
            continue
        bucket[0] += 1
        bucket[1] += n
        bucket[2] += cost
    shipped = set(orders)
    no_label = [k for k, (day, _) in dates.items() if day >= start and k not in shipped]
    info = {
        "basis": "order_date",
        "orders_with_labels": len(orders) - earlier[0] - unmatched[0],
        "orders_multi_label": multi,
        "earlier_orders": {"orders": earlier[0], "labels": earlier[1], "cost": round(earlier[2], 2)},
        "unmatched": {"orders": unmatched[0], "labels": unmatched[1], "cost": round(unmatched[2], 2)},
        "orders_without_label": len(no_label),
    }
    return days, info


def excluded_channel():
    """Sales and units from the excluded channel (Faire wholesale), used to take it out of every figure."""
    return load("raw/faire_sales.json")


def subtract_channel_sales(sales_daily, excl):
    """Daily ShopifyQL sales minus the excluded channel's daily sales."""
    if not excl:
        return sales_daily
    minus = {r["date"]: r for r in excl["daily"]}
    out = []
    for r in sales_daily:
        m = minus.get(r["date"])
        out.append({k: (round(v - m.get(k, 0), 2) if m and k != "date" else v) for k, v in r.items()})
    return out


def product_costs(start, cogs_cfg):
    """Per-day COGS from units ordered x unit cost, plus a per-product summary."""
    data = load("raw/units_by_product_daily.json")
    if not data:
        return {}, [], None
    compiled = [(re.compile(r["match"]), r) for r in cogs_cfg.get("rules", [])]
    rows = [dict(r) for r in data["rows"]]
    excl = excluded_channel()
    if excl:
        idx = {(r["date"], r["product"]): r for r in rows}
        for u in excl["units"]:
            r = idx.get((u["date"], u["product"]))
            if r is None:
                raise SystemExit(f"excluded-channel row not found in units data: {u}")
            r["qty"] -= u["qty"]
            r["net_sales"] = round(r["net_sales"] - u["net_sales"], 2)
    days = defaultdict(lambda: {"cogs": 0.0, "uncosted_sales": 0.0})
    products = {}
    for row in rows:
        if row["date"] < start or not (row["qty"] or row["net_sales"]):
            continue
        d = days[row["date"]]
        name = row["product"] or "(no product name)"
        rule = next((r for rx, r in compiled if rx.search(row["product"])), None)
        cost = rule["cost"] if rule else None
        if cost is not None:
            d["cogs"] += row["qty"] * cost
        else:
            d["uncosted_sales"] += row["net_sales"]
        p = products.setdefault(name, {"product": name, "group": rule["label"] if rule else None,
                                       "unit_cost": cost, "qty": 0, "net_sales": 0.0})
        p["qty"] += row["qty"]
        p["net_sales"] += row["net_sales"]
    summary = [{**p, "net_sales": round(p["net_sales"], 2),
                "cogs": round(p["qty"] * p["unit_cost"], 2) if p["unit_cost"] is not None else None}
               for p in products.values() if p["qty"] or p["net_sales"]]
    summary.sort(key=lambda p: -p["net_sales"])
    return days, summary


def order_rollup(start):
    """Per-day cash movement from order transactions (by transaction date, store time)."""
    days = defaultdict(lambda: defaultdict(float))
    stats = {"orders": 0, "cancelled": 0, "gateways": defaultdict(float)}
    path = DATA / "raw" / "orders.jsonl"
    if not path.exists():
        return {}, stats
    skip = {o["name"] for o in (load("raw/faire_orders.json") or {"orders": []})["orders"]}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        if o["name"] in skip:
            stats["excluded"] = stats.get("excluded", 0) + 1
            continue
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
    excl = excluded_channel()
    sales = {**sales, "daily": subtract_channel_sales(sales["daily"], excl)}
    meta = load("meta_daily.json")
    camps = load("meta_campaigns.json", {"campaigns": []})
    payouts = load("shopify_payouts.json")
    labels = load("shopify_labels.json", {"daily": [], "by_service": [], "by_package": []})
    label_day, label_info = labels_by_order_date(start, labels["daily"])
    cash, stats = order_rollup(start)
    cogs_day, cogs_products = product_costs(start, cfg.get("cogs", {}))

    spend = {r["date"]: r["spend"] for r in meta["daily"]}
    days = sorted({r["date"] for r in sales["daily"]} | set(spend) | set(cash) | set(label_day))
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
            "labels": label_day.get(day, {}).get("labels", 0),
            "label_cost": round(label_day.get(day, {}).get("cost", 0.0), 2),
            "label_orders": label_day.get(day, {}).get("orders", 0),
            "cogs": round(cogs_day.get(day, {}).get("cogs", 0.0), 2),
            "uncosted_sales": round(cogs_day.get(day, {}).get("uncosted_sales", 0.0), 2),
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
        "label_services": labels["by_service"],
        "label_packages": labels["by_package"],
        "cogs_products": cogs_products,
        "excluded": {"channel": excl["channel"], "orders": sum(r["orders"] for r in excl["daily"]),
                     "gross": round(sum(r["gross_sales"] for r in excl["daily"]), 2),
                     "net": round(sum(r["net_sales"] for r in excl["daily"]), 2),
                     "units": sum(u["qty"] for u in excl["units"])} if excl else None,
        "label_info": {**label_info,
                       "purchased_in_window": {"labels": sum(r["labels"] for r in labels["daily"]),
                                               "cost": round(sum(r["cost"] for r in labels["daily"]), 2)}},
        "order_stats": stats,
        "meta_account_totals": meta.get("account_totals"),
    }

    template = (ROOT / "dashboard" / "template.html").read_text()
    out = template.replace("/*__DATA__*/null", json.dumps(bundle, separators=(",", ":")))
    (ROOT / "dist").mkdir(exist_ok=True)
    (ROOT / "dist" / "index.html").write_text(out)

    tot = lambda k: round(sum(d[k] for d in daily), 2)
    print("label basis:", json.dumps(bundle["label_info"]))
    print(f"Built dist/index.html  {start} → {bundle['end']}  ({len(daily)} days)")
    for k in ("orders", "gross", "discounts", "returns", "net", "shipping", "collected",
              "collected_other", "refunds_paid", "fees", "spend", "labels", "label_cost", "cogs", "uncosted_sales"):
        print(f"  {k:16} {tot(k):>12,.2f}")


if __name__ == "__main__":
    main()
