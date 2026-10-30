-- 249: the storefront's own title for the proven variant, on enrichment_cart_variant_proofs (mig 248).
--
-- Option 2 (PR B review, 2026-09-29). The cart-link lane buys ONE Shopify variant, and the buyer never
-- picks it on that lane, so the purchase has to say which shade or size it buys. The proof job
-- (jobs/enrichment_cart_variant_proof.py) records the live variant's `title` ("NC50 / 1 fl oz") read
-- from the same response as the rest of the proof, cleaned by THE cart-link title rule
-- (services.shopify_variant_identity.clean_variant_title, #2462). PR C reads it for display only;
-- nothing decides what is bought from it. NULL when the job identified no variant.
--
-- Migrations do not self-apply in prod: db/enrichment_cart_variant_proofs.ensure_table() runs this same
-- ALTER after its CREATE, and db/schema_guard.py heals it at boot when the table exists.

ALTER TABLE enrichment_cart_variant_proofs ADD COLUMN IF NOT EXISTS variant_title TEXT;
