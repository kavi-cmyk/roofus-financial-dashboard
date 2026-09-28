# Roofus financial dashboard

One page that shows Roofus's money since **May 14, 2026**:

- Shopify orders, gross sales, discounts, refunds, net sales, and shipping charged to customers
- Net payouts: cash captured through Shopify Payments, minus refunds paid, minus processing fees
- Meta ad spend (daily and by campaign) for ad account `Roofus` (act_1210215467866079)
- Shopify cost as OPEX (plan and apps, pro-rated per day)
- Shopify Shipping label cost per day, by carrier/service and by package
- Product cost (COGS): units sold × unit cost from `config.json`
- **Contribution** = cash from Shopify − product cost − Meta spend − shipping labels − Shopify OPEX

Filter by the whole period, the last 30 days, or a single month. The page also has a monthly table and MER (net sales ÷ Meta spend).

## Layout

```
data/
  config.json               start date, Shopify plan fee, apps, product unit costs
  shopify_sales_daily.json  ShopifyQL daily sales
  raw/orders.jsonl          every order with its payment transactions and fees
  meta_daily.json           Meta spend per day
  meta_campaigns.json       Meta spend per campaign
  shopify_labels.json       Shopify Shipping labels: count and cost per day, by service, by package
dashboard/template.html     the dashboard (data is inlined at build time)
scripts/sync.py             pulls fresh data from the Shopify Admin API and the Meta Marketing API
scripts/build.py            combines data/ into dist/index.html
```

## Refresh the data

```bash
export SHOPIFY_STORE=3ywjyz-hj.myshopify.com
export SHOPIFY_ADMIN_TOKEN=shpat_...      # custom app: read_orders, read_reports, read_shopify_payments_payouts
export META_ACCESS_TOKEN=EAA...           # system user token with ads_read
python3 scripts/sync.py                   # writes data/*, then builds dist/index.html
```

Only the Python standard library is needed (3.9+). To rebuild from the data already in `data/` without calling any API, run `python3 scripts/build.py`.

Create the Shopify token under **Settings → Apps → Develop apps**. Orders older than 60 days also need the `read_all_orders` scope.

## What's measured and what's entered

| Figure | Source |
|---|---|
| Orders, gross sales, discounts, refunds, shipping charged | Shopify Analytics (`FROM sales`), by day in Pacific time |
| Processing fees, refunds paid, cash captured | Order transactions (`orders.transactions.fees`) |
| Net payouts | Estimated from the transactions above. If the token has `read_shopify_payments_payouts`, `sync.py` also saves the real payout ledger to `data/shopify_payouts.json` |
| Meta ad spend | Meta Marketing API insights, `time_increment=1` |
| Shopify plan and apps | Entered in `config.json` or on the page. Grow plan: $105/mo billed monthly, $79/mo billed annually |
| Shipping labels | Shopify Analytics (`FROM shipping_labels`), matched to orders and counted on the order date |
| Product cost (COGS) | Units ordered per product per day (`FROM sales ... quantity_ordered`) × `cogs.rules` in `config.json`. Wipes $1.26/canister, poop bags $1.19 (Mystery Gift), paw cleanser $0.82 (gift) |

Values typed into the page's "Costs you enter" panel are saved in that browser only. To change the default for everyone, edit `data/config.json` and rebuild.

Contribution doesn't include 3PL, packaging or inventory write-offs. Products without a rule in `cogs.rules` (ear/eye wipes, shampoo, paw care, pet wipes, Faire wholesale) count as $0 cost until you add one.

## Deploy on Vercel

The repo includes `vercel.json`: no build step, and Vercel serves `dist/`. In Vercel, choose **Add New → Project**, import `kavi-cmyk/roofus-financial-dashboard`, and deploy. After each sync, commit the new `dist/index.html` and Vercel redeploys it.

Without protection, the production URL is public. To limit who can open it, turn on **Settings → Deployment Protection** in Vercel.
