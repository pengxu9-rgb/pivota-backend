# Cafe24 Native Adapter

The Cafe24 adapter reuses Pivota's canonical catalog and commerce-event layers.
It does not create a Cafe24-specific analytics store.

## Configuration

The OAuth path uses:

- `CAFE24_CLIENT_ID`
- `CAFE24_CLIENT_SECRET`
- `CAFE24_REDIRECT_URI`
- `CAFE24_WEBHOOK_API_KEY` — the verification code Cafe24 sends as `X-API-Key`
- `CAFE24_OAUTH_STATE_SECRET` — optional when the normal JWT signing secret exists
- `CAFE24_API_VERSION` — defaults to the current documented version `2026-03-01`
- `CAFE24_SCOPES` — defaults to the minimum catalog/order/application scopes

Start a merchant install with:

```text
GET /integrations/cafe24/oauth/start?merchant_id=...&mall_id=...
```

An operator may also connect already-issued tokens with
`POST /integrations/cafe24/connect`. Access and rotating refresh tokens are stored
inside the existing `merchant_stores.api_key` credential blob. The catalog adapter
refreshes an expiring two-hour access token and persists the replacement refresh
token before pulling products. After connection, Pivota calls
`PUT /admin/webhooks/setting` with `reception_status=T` to activate reception for
the subscriptions already configured on the app.

## Catalog

`Cafe24ProductAdapter` is registered in `PLATFORM_ADAPTERS`, so the existing
Universal Product Sync path can fetch Cafe24 products. Cafe24 remains excluded
from the live SKU-match/PDP renderability allowlists until a merchant pilot has
validated URL, inventory, and orderability behavior end to end. The adapter uses:

```text
GET https://{mall_id}.cafe24api.com/api/v2/admin/products
    ?since_product_no=...
    &limit=100
    &embed=variants
```

The mapper emits `StandardProduct` / `StandardProductVariant`; Cafe24 identifiers
remain platform identifiers rather than being mislabeled as Pivota canonical IDs.

## Webhooks and Data Bridge

Configure both Cafe24 app webhooks and Data Bridge to send to:

```text
POST /webhooks/cafe24
X-API-Key: <Cafe24 webhook verification code>
X-Trace-ID: <Cafe24 trace id>
```

Supported mappings:

| Cafe24 source | Canonical event |
| --- | --- |
| Data Bridge `VIEW_CONTENT` | `product.viewed` |
| Data Bridge `INITIATE_ORDERFORM` | `checkout.started` |
| Data Bridge `CREATE_ORDER` | `order.created` |
| Webhook `90023` | `order.created` and, when paid, `order.paid` |
| Webhook `90025` | payment status → `order.paid` / `payment.authorized` / `payment.failed` |
| Webhook `90026`, `90072` | `order.cancelled` |
| Webhook `90027`, `90028`, `90074` | `return.created` |
| Webhook `90029`, `90073` | `refund.created` / `refund.succeeded` |
| Webhook `90084` | `cart.item_added` |

Data Bridge `CVID` stitches product and checkout activity. Order, payment, return,
and refund identifiers bridge the server-side lifecycle. The webhook trace id is
part of each upstream idempotency key.

Cafe24's verified native `90084` event covers add-to-cart without ScriptTag
heuristics and provides native product, variant, and quantity fields. Cafe24 does
not provide the same native signal for remove/update, so those remain optional
Universal Web Collector coverage rather than being inferred from brittle theme
click selectors. Bulk cancellation/refund/return events are expanded into one
canonical event per comma-separated order ID.

The adapter deliberately allowlists telemetry fields. Buyer names, email addresses,
phone numbers, billing details, and bank account numbers from Cafe24 order payloads
are not stored in commerce-event metadata.

Cafe24 recommends API reconciliation because webhooks can be delayed or missed.
Pivota therefore exposes:

```text
GET  /integrations/cafe24/{store_id}/status
POST /integrations/cafe24/{store_id}/webhook-reception/enable
POST /integrations/cafe24/{store_id}/reconcile?lookback_days=7&limit_per_stream=500
```

Reconciliation incrementally reads both `GET /admin/webhooks/logs` and
`GET /admin/databridge/logs`, replays their allowlisted request bodies through the
same canonical mapper, and stores separate cursors in the existing credential
blob. Replays and concurrent runs are safe because the commerce ledger remains
idempotent.

Cafe24 does not expose an Admin REST endpoint for creating event subscriptions or
setting their receiving URL. The event numbers and Data Bridge events must still
be configured once in Developer Center > App Setup; the status response makes this
boundary explicit. The existing isolated APScheduler invokes a bounded,
least-recently-run store batch every 15 minutes. It remains a no-op until
`CAFE24_RECONCILIATION_ENABLED=true`; batch size, lookback, and per-stream limit
are independently configurable:

```text
CAFE24_RECONCILIATION_BATCH_SIZE=10
CAFE24_RECONCILIATION_LOOKBACK_DAYS=7
CAFE24_RECONCILIATION_LIMIT_PER_STREAM=500
```

## Official references

- https://developers.cafe24.com/en/app/front/app/develop/oauth/oauthcode
- https://developers.cafe24.com/en/app/front/app/develop/oauth/retoken
- https://developers.cafe24.com/en/app/front/app/develop/webhook/manage
- https://developers.cafe24.com/en/app/front/app/develop/webhook/sample
- https://developers.cafe24.com/en/data/front/cafe24bridge/setWebhook/sampleWebhook
- https://developers.cafe24.com/docs/en/api/admin/
