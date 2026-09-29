-- Reverses 248_enrichment_cart_variant_proofs.sql. Nothing reads or writes the table as of PR A, so dropping it
-- changes no behaviour. Once PR C reads it, a missing table means no enrichment row has a proof, and the
-- cart-link lane refuses every one of them (it fails closed).
--
-- NOT STICKY ON ITS OWN once a caller exists: db/enrichment_cart_variant_proofs.ensure_table() recreates it at
-- first use. Remove that call in the same release if the table must stay gone.
DROP TABLE IF EXISTS enrichment_cart_variant_proofs;
