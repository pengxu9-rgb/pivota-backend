-- THE THREE TABLES THE AGENTIC PURCHASE **ROUTES** NEED (WP4). None of them belongs to the
-- state machine: migrations 224/225 own the purchase and the enrollment, and nothing here is
-- read or written by services/reap_agentic_purchase.py or jobs/reap_agentic_purchase_poll.py.
-- These three exist because a ROUTE has three questions the ledger cannot answer:
--
--   1. may this merchant be bought from, in this buyer's market?   reap_agentic_eligibility
--   2. what opaque reference does Reap know this buyer by?         reap_agentic_buyer_refs
--   3. have I already started this exact purchase?                 reap_agentic_purchase_keys
--
-- WHY ELIGIBILITY IS A TABLE AND NOT A DIAL. `REAP_AGENTIC_ENABLED` arms the RAIL; it says
-- nothing about WHICH merchants are transactable on it, and that set is per-merchant,
-- per-market, and changes without a deploy. An env var holding a comma-separated domain list
-- would be the same data with no audit trail, no per-row aliases, and a length limit. The
-- important half is that it is an ALLOWLIST: a domain with no row here is refused. There is no
-- "unknown means allowed" arm anywhere in routes/agent_commerce_reap.py, because the thing being
-- allowed is spending a buyer's own card at a third party.
--
-- WHY THE PRIMARY KEY CARRIES product_key AND variant_key, AND WHY THEY ARE '' AND NOT NULL.
-- One table serves two shapes of assertion:
--
--   the MERCHANT row   (product_key = '', variant_key = '')  carries `market_country` and
--                      `enabled`. This is the row the eligibility decision is made against.
--   an OVERRIDE row    (a real product_key, and optionally a real variant_key) carries the two
--                      alias lists for ONE product. It does not grant eligibility and is never
--                      consulted for it — a product override on a merchant with no enabled
--                      merchant row buys nothing.
--
-- The sentinel is `''` rather than NULL because this table is built on BOTH dialects and NULL in
-- a PRIMARY KEY does not mean the same thing on each: Postgres forbids it outright, and SQLite
-- allows it while treating every NULL as distinct — so a NULL-keyed "merchant row" would be
-- insertable twice on SQLite and not at all on Postgres. A sentinel that is a VALUE behaves
-- identically on both, and `''` cannot collide with a real key because a blank product_key is
-- refused by the route before it ever reaches a lookup.
--
-- WHY market_country IS ON THE MERCHANT ROW AND THE MATCH IS EQUALITY. Domestic only. Reap's
-- agentic rail is transactable where the buyer, the card and the storefront are in one market;
-- a cross-border attempt does not fail cleanly, it quotes in another currency against another
-- price. The route compares the buyer's `shipping_address.country` to THIS column and refuses
-- `merchant_not_eligible` on a mismatch. Two rows for one domain in two markets is the supported
-- way to say "this merchant sells in both", and the composite key permits exactly that.
--
-- WHY THE ALIASES ARE jsonb AND NOT text[]. Same reason migration 225 gives at length: this rail
-- is raw SQL with no SQLAlchemy result processor in front of it, so an array type arrives as a
-- driver-specific shape on one dialect and as nothing at all on the other. jsonb has a SQLite
-- twin (TEXT holding JSON) that the route decodes with one code path. NULL means "no aliases",
-- not `[]`.
--
-- WHY THE BUYER REF IS A THIRD IDENTIFIER AND NOT ONE OF THE TWO WE ALREADY HAVE. What we send
-- Reap as `owner.id` leaves our system, so it must not be a key into the rest of it:
--
--   buyer_id                 is the global buyer. It joins to shop_users, to addresses, to
--                            orders. Sending it to a partner exports the join.
--   agent_scoped_buyer_ref   (db/buyer_vault.buyer_agent_links) is already scoped to ONE agent.
--                            An enrollment is a CARD, and a buyer's card does not belong to the
--                            agent that happened to be in the room when they enrolled it — two
--                            agents would mint two refs, and the "at most one active enrollment
--                            per buyer_ref" invariant in migration 224 would then hold twice,
--                            per agent, which is not the invariant anybody wanted.
--
-- So: one opaque ref per BUYER, minted from db/buyer_vault.mint_pairwise_buyer_ref()'s 128 bits,
-- stable for the life of the buyer, and never returned to any caller. It is written once and
-- read thereafter; the route treats a unique violation as "somebody else minted it first" and
-- re-reads, which is the only race this table has.
--
-- WHY IDEMPOTENCY IS ITS OWN TABLE. `reap_agentic_purchases` has no column for a client key and
-- adding one would put a caller-supplied string inside the ledger's INSERT — a table whose
-- entire header is about what may and may not live next to the buyer's address. The alternative
-- considered and rejected was refusing a duplicate key outright (409): that turns an agent's
-- RETRY of a request whose response it never saw into a hard failure, which is precisely the
-- case idempotency exists for. The key lives beside the ledger, points at a purchase id, and the
-- route honours it for 24 hours (the window is enforced in the route, not here, because it is a
-- policy and this is storage). The key row also carries a hash of the request it was used for,
-- so that reusing a key on a DIFFERENT body is a refusal rather than a 202 about somebody else's
-- purchase -- see the column.

CREATE TABLE IF NOT EXISTS reap_agentic_eligibility (
    merchant_domain VARCHAR(255) NOT NULL,

    -- '' on the merchant row; a real key on an override row. See the header.
    product_key TEXT NOT NULL DEFAULT '',
    variant_key TEXT NOT NULL DEFAULT '',

    -- ISO-3166-1 alpha-2, UPPERCASE. The buyer's market must equal this exactly.
    market_country VARCHAR(2) NOT NULL CHECK (market_country ~ '^[A-Z]{2}$'),

    -- DEFAULT FALSE: a row that somebody created and did not finish thinking about is not an
    -- authorization to spend. Turning a merchant on is a deliberate UPDATE.
    enabled BOOLEAN NOT NULL DEFAULT FALSE,

    -- Per-row resolution aliases, merged into PurchaseRow by the route. NULL means none.
    accept_variant_labels JSONB,
    also_accept_domains JSONB,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- market_country IS IN THE KEY, and it has to be. The header says two rows for one domain
    -- in two markets is how a merchant that sells in both is expressed; a key without this
    -- column makes that statement UNINSERTABLE — the second market collides with the first on
    -- the merchant row. Caught by
    -- tests/test_agent_commerce_reap_routes.py::test_the_market_comes_from_the_shipping_country_
    -- not_from_the_caller, which seeds US and CA for one domain.
    --
    -- It also makes this key the index the eligibility read wants: that read is
    -- `merchant_domain = ? AND market_country = ? AND product_key IN (?, ?)`, which is this key
    -- left to right, so no second index is needed and none is created.
    PRIMARY KEY (merchant_domain, market_country, product_key, variant_key)
);


CREATE TABLE IF NOT EXISTS reap_agentic_buyer_refs (
    buyer_id VARCHAR(50) PRIMARY KEY,

    -- What Reap knows this buyer as. UNIQUE because two buyers sharing one owner id would share
    -- an enrollment, and an enrollment is a card.
    reap_buyer_ref VARCHAR(128) NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_reap_agentic_buyer_refs_ref
    ON reap_agentic_buyer_refs (reap_buyer_ref);


CREATE TABLE IF NOT EXISTS reap_agentic_purchase_keys (
    -- The OWNERSHIP PAIR again, and for the same reason migration 224 gives: an idempotency key
    -- is a caller-chosen string, so two agents — or two end users of one agent — can and will
    -- pick the same one. Keyed on the pair, "the same key" means the same key FROM THE SAME
    -- BUYER, and one caller's retry can never return another caller's purchase.
    agent_id VARCHAR(128) NOT NULL,
    agent_user_ref_hash VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,

    -- FK BY VALUE, like enrollment_id on the purchase row: a real REFERENCES would make deleting
    -- purchase history fail on a key nobody is going to replay anyway.
    purchase_id VARCHAR(64) NOT NULL,

    -- WHAT THE KEY WAS USED FOR. sha256 over the canonical form of the fields that DECIDE the
    -- purchase: merchant, product, variant, quantity, buyer email, shipping address, return url.
    --
    -- WITHOUT THIS COLUMN AN IDEMPOTENCY KEY IS A LIE THE SERVER TELLS. A key alone answers "have
    -- I seen this key?", and every caller reads the 202 as "your request was carried out". Reuse
    -- a key on a different body — a different size, a different address, a different product,
    -- which is exactly what a client that derives keys from a session id or a cart id will do —
    -- and the answer is a 202 naming a purchase of something else. The buyer then approves a
    -- hosted page for a thing they did not ask for, and nothing anywhere records that two
    -- different requests were made.
    --
    -- So the key is the question and this is the request it was asked about: same key + same
    -- hash replays, same key + DIFFERENT hash is `idempotency_conflict` (409). That is the
    -- behaviour every payment API worth copying has, and the column is what makes it expressible.
    --
    -- NOT NULL WITH NO DEFAULT: there is no such thing as a key row whose request is unknown, and
    -- a nullable column would let one be written by a path that forgot.
    request_hash VARCHAR(64) NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (agent_id, agent_user_ref_hash, idempotency_key)
);
