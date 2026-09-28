#!/usr/bin/env python3
"""Pull fresh data from Shopify and Meta into data/, then run build.py.

Environment:
  SHOPIFY_STORE          e.g. 3ywjyz-hj.myshopify.com
  SHOPIFY_ADMIN_TOKEN    Admin API access token from a custom app with scopes:
                         read_orders, read_reports, read_shopify_payments_payouts
                         (read_all_orders if the window is older than 60 days)
  META_ACCESS_TOKEN      System-user or long-lived token with ads_read
  META_AD_ACCOUNT_ID     numeric id, defaults to config.json

Usage:  python3 scripts/sync.py [--until YYYY-MM-DD]
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SHOPIFY_API = "2025-07"
META_API = "v23.0"


def http_json(url, body=None, headers=None, retries=4):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise SystemExit(f"{e.code} from {url.split('?')[0]}: {e.read()[:500]!r}")


# ---------------------------------------------------------------- Shopify
def local_date(iso):
    from zoneinfo import ZoneInfo
    ts = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return ts.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()


def shopify(query, variables=None):
    store, token = os.environ["SHOPIFY_STORE"], os.environ["SHOPIFY_ADMIN_TOKEN"]
    res = http_json(f"https://{store}/admin/api/{SHOPIFY_API}/graphql.json",
                    {"query": query, "variables": variables or {}},
                    {"X-Shopify-Access-Token": token})
    if res.get("errors"):
        raise RuntimeError(json.dumps(res["errors"])[:800])
    return res["data"]


def shopifyql(ql):
    q = """query($q:String!){ shopifyqlQuery(query:$q){ tableData{ columns{name} rows } parseErrors } }"""
    res = shopify(q, {"q": ql})["shopifyqlQuery"]
    if res.get("parseErrors"):
        raise RuntimeError(res["parseErrors"])
    cols = [c["name"] for c in res["tableData"]["columns"]]
    return [dict(zip(cols, row if isinstance(row, list) else [row[c] for c in cols]))
            for row in res["tableData"]["rows"]]


def num(v):
    return float(v) if v not in (None, "") else 0.0


def sync_sales(start, until):
    ql = (f"FROM sales SHOW orders, gross_sales, discounts, returns, net_sales, shipping_charges, "
          f"taxes, total_sales TIMESERIES day SINCE {start} UNTIL {until}")
    daily = [{"date": str(r["day"])[:10], "orders": int(num(r["orders"])),
              **{k: num(v) for k, v in r.items() if k not in ("day", "orders")}} for r in shopifyql(ql)]
    (DATA / "shopify_sales_daily.json").write_text(json.dumps(
        {"source": "ShopifyQL: " + ql, "pulled_at": str(dt.date.today()), "daily": daily}, indent=1))
    print(f"shopify sales: {len(daily)} days")


FAIRE_Q = """query($after:String,$q:String!){ orders(first:100, after:$after, query:$q) {
  nodes { name createdAt cancelledAt lineItems(first:100){ nodes { title sku quantity } } }
  pageInfo{ hasNextPage endCursor } } }"""


def sync_faire(start, until, channel="Faire: Sell Wholesale"):
    """Faire wholesale: its daily sales and units (subtracted from every figure) and its order names."""
    where = f"WHERE sales_channel = '{channel}'"
    span = f"SINCE {start} UNTIL {until}"
    daily = [{"date": str(r["day"])[:10], "orders": int(num(r["orders"])),
              **{k: num(r[k]) for k in ("gross_sales", "discounts", "returns", "net_sales", "shipping_charges", "taxes", "total_sales")}}
             for r in shopifyql(f"FROM sales SHOW orders, gross_sales, discounts, returns, net_sales, shipping_charges, taxes, "
                                f"total_sales {where} GROUP BY day {span} HAVING orders != 0 OR net_sales != 0 OR returns != 0 "
                                f"ORDER BY day ASC")]
    units = [{"date": str(r["day"])[:10], "product": r["product_title"] or "", "qty": int(num(r["quantity_ordered"])),
              "net_sales": num(r["net_sales"])}
             for r in shopifyql(f"FROM sales SHOW quantity_ordered, net_sales {where} GROUP BY day, product_title {span} "
                                f"HAVING quantity_ordered != 0 OR net_sales != 0 ORDER BY day ASC LIMIT 5000")]
    (DATA / "raw" / "faire_sales.json").write_text(json.dumps(
        {"channel": channel, "source": f"ShopifyQL FROM sales {where}", "daily": daily, "units": units}, indent=1, ensure_ascii=False))
    orders, after = [], None
    while True:
        page = shopify(FAIRE_Q, {"after": after, "q": f"created_at:>={start} source_name:faire"})["orders"]
        for o in page["nodes"]:
            orders.append({"name": o["name"], "date": local_date(o["createdAt"]), "cancelled": bool(o["cancelledAt"]),
                           "lines": [[li["title"], li["sku"] or "", li["quantity"]] for li in o["lineItems"]["nodes"]]})
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    (DATA / "raw" / "faire_orders.json").write_text(json.dumps(
        {"source": "Shopify Admin GraphQL: Faire orders with line items", "orders": orders}, indent=1, ensure_ascii=False))
    print(f"faire orders: {len(orders)}")


def sync_units(start, until):
    """Units and net sales per product per day, one query per month."""
    rows, month = [], dt.date.fromisoformat(start).replace(day=1)
    end = dt.date.fromisoformat(until)
    while month <= end:
        nxt = (month + dt.timedelta(days=32)).replace(day=1)
        lo, hi = max(month, dt.date.fromisoformat(start)), min(nxt - dt.timedelta(days=1), end)
        for r in shopifyql(f"FROM sales SHOW quantity_ordered, net_items_sold, net_sales GROUP BY product_title "
                           f"TIMESERIES day SINCE {lo} UNTIL {hi} LIMIT 5000"):
            qty, items, sales = int(num(r["quantity_ordered"])), int(num(r["net_items_sold"])), num(r["net_sales"])
            if qty or items or sales:
                rows.append({"date": str(r["day"])[:10], "product": r["product_title"] or "",
                             "qty": qty, "net_items": items, "net_sales": sales})
        month = nxt
    (DATA / "raw").mkdir(exist_ok=True)
    (DATA / "raw" / "units_by_product_daily.json").write_text(json.dumps(
        {"source": "ShopifyQL FROM sales SHOW quantity_ordered, net_items_sold, net_sales GROUP BY product_title TIMESERIES day",
         "rows": rows}, indent=1, ensure_ascii=False))
    print(f"units by product: {len(rows)} rows")


def sync_labels(start, until):
    (DATA / "raw").mkdir(exist_ok=True)
    span = f"SINCE {start} UNTIL {until}"
    daily = [{"date": str(r["day"])[:10], "labels": int(num(r["shipping_labels"])), "cost": num(r["shipping_label_costs"])}
             for r in shopifyql(f"FROM shipping_labels SHOW shipping_labels, shipping_label_costs TIMESERIES day {span}")]
    services = [{"carrier": r["shipping_carrier"], "service": r["shipping_service"],
                 "labels": int(num(r["shipping_labels"])), "cost": num(r["shipping_label_costs"])}
                for r in shopifyql(f"FROM shipping_labels SHOW shipping_labels, shipping_label_costs "
                                   f"GROUP BY shipping_carrier, shipping_service {span} ORDER BY shipping_label_costs DESC")]
    packages = [{"package": r["package_name"], "labels": int(num(r["shipping_labels"])), "cost": num(r["shipping_label_costs"])}
                for r in shopifyql(f"FROM shipping_labels SHOW shipping_labels, shipping_label_costs "
                                   f"GROUP BY package_name {span} ORDER BY shipping_label_costs DESC")]
    (DATA / "shopify_labels.json").write_text(json.dumps(
        {"source": "ShopifyQL: FROM shipping_labels", "pulled_at": str(dt.date.today()),
         "daily": daily, "by_service": services, "by_package": packages}, indent=1))
    print(f"shipping labels: {sum(r['labels'] for r in daily)} labels, ${sum(r['cost'] for r in daily):,.2f}")

    # Per order, one query per label-purchase month to stay under the row limit.
    rows, month = [], dt.date.fromisoformat(start).replace(day=1)
    end = dt.date.fromisoformat(until)
    while month <= end:
        nxt = (month + dt.timedelta(days=32)).replace(day=1)
        lo, hi = max(month, dt.date.fromisoformat(start)), min(nxt - dt.timedelta(days=1), end)
        for r in shopifyql(f"FROM shipping_labels SHOW shipping_labels, shipping_label_costs GROUP BY order_name "
                           f"SINCE {lo} UNTIL {hi} ORDER BY order_name ASC LIMIT 5000"):
            rows.append({"order_name": r["order_name"], "label_month": str(month)[:7],
                         "labels": int(num(r["shipping_labels"])), "cost": num(r["shipping_label_costs"])})
        month = nxt
    (DATA / "raw" / "labels_by_order.json").write_text(json.dumps(
        {"source": "ShopifyQL FROM shipping_labels GROUP BY order_name, one query per label-purchase month",
         "rows": rows}, indent=1))
    print(f"shipping labels by order: {len(rows)} rows")


ORDERS_Q = """query($after:String,$q:String!){ orders(first:100, after:$after, query:$q, sortKey:CREATED_AT) {
  nodes { name createdAt displayFinancialStatus cancelledAt
    currentTotalPriceSet{shopMoney{amount}} totalReceivedSet{shopMoney{amount}}
    totalRefundedSet{shopMoney{amount}} totalShippingPriceSet{shopMoney{amount}}
    transactions(first:20){ kind status gateway processedAt amountSet{shopMoney{amount}} fees{ amount{amount} } } }
  pageInfo{ hasNextPage endCursor } } }"""


def sync_orders(start):
    out, after, n = [], None, 0
    while True:
        page = shopify(ORDERS_Q, {"after": after, "q": f"created_at:>={start}"})["orders"]
        for o in page["nodes"]:
            m = lambda k: float(o[k]["shopMoney"]["amount"])
            out.append(json.dumps({
                "name": o["name"], "createdAt": o["createdAt"], "financialStatus": o["displayFinancialStatus"],
                "cancelled": bool(o["cancelledAt"]), "total": m("currentTotalPriceSet"),
                "received": m("totalReceivedSet"), "refunded": m("totalRefundedSet"),
                "shipping": m("totalShippingPriceSet"),
                "txns": [{"kind": t["kind"], "status": t["status"], "gateway": t["gateway"],
                          "processedAt": t["processedAt"], "amount": float(t["amountSet"]["shopMoney"]["amount"]),
                          "fee": round(sum(float(f["amount"]["amount"]) for f in t["fees"]), 2)}
                         for t in o["transactions"]]}, separators=(",", ":")))
        n += 1
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    (DATA / "raw").mkdir(exist_ok=True)
    (DATA / "raw" / "orders.jsonl").write_text("\n".join(out) + "\n")
    print(f"shopify orders: {len(out)} in {n} pages")


PAYOUTS_Q = """query($after:String,$q:String!){ shopifyPaymentsAccount { payouts(first:100, after:$after, query:$q, sortKey:ISSUED_AT) {
  nodes { issuedAt status transactionType net{amount}
    summary{ chargesGross{amount} chargesFee{amount} refundsFeeGross{amount} refundsFee{amount}
             adjustmentsGross{amount} adjustmentsFee{amount} } }
  pageInfo{ hasNextPage endCursor } } } }"""


def sync_payouts(start):
    rows, after = [], None
    try:
        while True:
            acct = shopify(PAYOUTS_Q, {"after": after, "q": f"issued_at:>={start}"})["shopifyPaymentsAccount"]
            page = acct["payouts"]
            for p in page["nodes"]:
                s = p["summary"]
                f = lambda k: float(s[k]["amount"])
                sign = -1 if p["transactionType"] == "WITHDRAWAL" else 1
                rows.append({"date": p["issuedAt"][:10], "status": p["status"],
                             "net": sign * float(p["net"]["amount"]),
                             "charges": f("chargesGross"), "refunds": f("refundsFeeGross"),
                             "adjustments": f("adjustmentsGross"),
                             "fees": f("chargesFee") + f("refundsFee") + f("adjustmentsFee")})
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
    except RuntimeError as e:
        print(f"payouts skipped (token lacks read_shopify_payments_payouts?): {e}", file=sys.stderr)
        return
    (DATA / "shopify_payouts.json").write_text(json.dumps({"payouts": rows}, indent=1))
    print(f"shopify payouts: {len(rows)}")


# ---------------------------------------------------------------- Meta
def meta_insights(account, start, until, **params):
    token = os.environ["META_ACCESS_TOKEN"]
    q = {"access_token": token, "time_range": json.dumps({"since": start, "until": until}), "limit": 500, **params}
    url = f"https://graph.facebook.com/{META_API}/act_{account}/insights?" + urllib.parse.urlencode(q)
    rows = []
    while url:
        res = http_json(url)
        rows += res.get("data", [])
        url = res.get("paging", {}).get("next")
    return rows


def sync_meta(account, start, until):
    daily = meta_insights(account, start, until, fields="spend", time_increment=1, level="account")
    total = meta_insights(account, start, until, fields="spend,impressions,clicks", level="account")
    camps = meta_insights(account, start, until, fields="campaign_name,spend,impressions,clicks", level="campaign")
    t = total[0] if total else {}
    (DATA / "meta_daily.json").write_text(json.dumps({
        "source": f"Meta Marketing API act_{account}", "pulled_at": str(dt.date.today()),
        "account_totals": {"spend": float(t.get("spend", 0)), "impressions": int(t.get("impressions", 0)),
                           "clicks": int(t.get("clicks", 0))},
        "daily": [{"date": r["date_start"], "spend": float(r["spend"])} for r in daily]}, indent=1))
    camps = sorted(({"name": r["campaign_name"], "spend": float(r["spend"]), "impressions": int(r["impressions"]),
                     "clicks": int(r.get("clicks", 0))} for r in camps if float(r["spend"]) > 0),
                   key=lambda r: -r["spend"])
    (DATA / "meta_campaigns.json").write_text(json.dumps({"source": "Meta Marketing API, campaign level",
                                                          "campaigns": camps}, indent=1))
    print(f"meta: {len(daily)} days, {len(camps)} campaigns, spend {t.get('spend')}")


def main():
    cfg = json.loads((DATA / "config.json").read_text())
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default=str(dt.date.today()))
    args = ap.parse_args()
    start = cfg["start_date"]
    sync_sales(start, args.until)
    sync_labels(start, args.until)
    sync_units(start, args.until)
    sync_faire(start, args.until)
    sync_orders(start)
    sync_payouts(start)
    sync_meta(os.environ.get("META_AD_ACCOUNT_ID", cfg["meta_ad_account_id"]), start, args.until)
    import build
    build.main()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main()
