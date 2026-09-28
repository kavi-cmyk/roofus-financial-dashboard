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
def shopify(query, variables=None):
    store, token = os.environ["SHOPIFY_STORE"], os.environ["SHOPIFY_ADMIN_TOKEN"]
    res = http_json(f"https://{store}/admin/api/{SHOPIFY_API}/graphql.json",
                    {"query": query, "variables": variables or {}},
                    {"X-Shopify-Access-Token": token})
    if res.get("errors"):
        raise RuntimeError(json.dumps(res["errors"])[:800])
    return res["data"]


def sync_sales(start, until):
    q = """query($q:String!){ shopifyqlQuery(query:$q){ tableData{ columns{name} rows } parseErrors } }"""
    ql = (f"FROM sales SHOW orders, gross_sales, discounts, returns, net_sales, shipping_charges, "
          f"taxes, total_sales TIMESERIES day SINCE {start} UNTIL {until}")
    res = shopify(q, {"q": ql})["shopifyqlQuery"]
    if res.get("parseErrors"):
        raise RuntimeError(res["parseErrors"])
    cols = [c["name"] for c in res["tableData"]["columns"]]
    daily = []
    for row in res["tableData"]["rows"]:
        r = dict(zip(cols, row if isinstance(row, list) else [row[c] for c in cols]))
        daily.append({"date": str(r["day"])[:10], "orders": int(float(r["orders"] or 0)),
                      **{k: float(r[k] or 0) for k in cols if k not in ("day", "orders")}})
    (DATA / "shopify_sales_daily.json").write_text(json.dumps(
        {"source": "ShopifyQL: " + ql, "pulled_at": str(dt.date.today()), "daily": daily}, indent=1))
    print(f"shopify sales: {len(daily)} days")


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
    sync_orders(start)
    sync_payouts(start)
    sync_meta(os.environ.get("META_AD_ACCOUNT_ID", cfg["meta_ad_account_id"]), start, args.until)
    import build
    build.main()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main()
