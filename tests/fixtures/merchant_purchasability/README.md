# Recorded checkout fixtures for the merchant purchasability detector

Three landed Shopify checkouts, fetched live on **2026-09-22** through the same code path the
sweep uses (`build_shopify_cart_permalink` + `_fetch_following`, `buyer=None`, `country=US`).
They are the evidence that the card detector separates a card checkout from a PayPal-only one.

| file | merchant | variant | what it proves |
|---|---|---|---|
| `idewcare_card.html` | idewcare.com | 46722440036604 | `PaymentProvider` / `shopify_payments`, brands VISA MASTERCARD AMEX DISCOVER DINERS_CLUB ELO -> `card_available=True`, line USD 13.99 |
| `judydoll_card.html` | judydoll.com | 49922977038613 | `PaymentProvider` / `Airwallex`, brands VISA MASTERCARD AMEX MAESTRO JCB UNIONPAY -> `card_available=True`, line USD 13.99 |
| `flowerbeauty_paypal_only.html` | flowerbeauty.com | 17281773207622 | NO `PaymentProvider` at all -> `card_available=False` / `NO_CARD_PAYMENT`, line **USD 8.00** against our indexed USD 14.95 |

The third file is the incident. It is the page that was served as purchasable.

## How they were trimmed

Each source page was ~310-370 KB. The trimming was mechanical, by
`scratchpad/wp6_k3n9/make_fixtures.py`, and did four things and no others:

1. Kept ONLY two JSON spans from the page: the `availablePaymentLines` array (the card evidence)
   and the first `merchandiseLines` array (the line price evidence). Everything else — all
   markup, all scripts, every other piece of serialized state — was dropped.
2. Re-emitted both spans **HTML-escaped**, exactly as the live page carries them, inside one
   `<script type="application/json">`. That is deliberate: the fixture therefore still exercises
   the `html.unescape` step the detector performs, rather than testing a pre-decoded shortcut.
3. Redacted bearer-ish values BY KEY — `clientToken`, `eligibilityToken`, `sessionToken`,
   `queueToken`, `token`, `storefrontAccessToken`, `merchantId`, `paymentMethodIdentifier` — and
   any JWT-shaped literal, replacing each with the string `REDACTED`. The checkout's own
   `cn/<token>` never appears because no URL was kept.
4. Nothing else was edited. The structures the detector reads — `placements`, `__typename`,
   `name`, `paymentBrands`, `merchandise.id`, `quantity`, `totalAmount` — are verbatim.

## PII

There is none, and not by redaction: the pages were fetched with `buyer=None`, so no email,
name, address or phone number ever existed in the source. The click id was our own synthetic
`clk_wp6survey`, tied to no buyer and no campaign, and it does not survive the trim. The only
personal-looking strings left are product titles and a merchant's vendor name.

## Refreshing them

Re-run the fetch with the same three variants and re-run the trimmer. If a merchant has changed
its payment configuration the fixture's expectation changes with it — update `LIVE_CASES` in
`tests/test_merchant_purchasability.py` and say in the commit message what the store now offers.
Do NOT hand-edit a fixture to keep a test green: these files are a recording, and a recording
somebody edited proves nothing.
