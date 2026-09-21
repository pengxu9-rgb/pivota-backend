# Merchant Store Readiness contract

`POST /api/merchant-center/audit/store-readiness` accepts one representative
HTTPS product URL. The authenticated merchant must own the URL's storefront
domain. Catalog synchronization is not required.

The route creates or reuses a merchant-owned `storefront` execution route and
enqueues `commerce_checkout_probe`. A second request while that route is
pending returns the existing run instead of dispatching duplicate browser
work.

`GET /api/merchant-center/audit/store-readiness` returns the latest journey at
six fixed steps: storefront access, store search, product detail, add to cart,
synthetic shipping address, and checkout. Missing evidence is `not_run`; it is
never projected as success. A legacy commerce receipt without step evidence is
therefore shown as needing attention.

The browser worker may fill Pivota-owned synthetic shipping data only. It does
not enter payment data and does not submit an order. Receipts contain a closed
vocabulary of step/status/reason values, not URLs, page text, cookies, form
values, or buyer information.
