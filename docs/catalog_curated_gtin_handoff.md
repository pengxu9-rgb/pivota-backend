# Captured GTIN at the enrichment identity gate

The two read-only A'PIEU Honey & Milk Lip Oil PDP observations in
`tests/fixtures/retailer_lip_oil_public_observations.json` share barcode
`8809530070499`, despite different retailer titles and native variant IDs.
The live bulk `/products.json` responses did not include that barcode; the
individual `.js` PDP responses did. Preserving seller IDs alone never proved
that these offers belonged to one shared content identity.

The combined pure-plan experiment exposed a second loss point:
`_build_pdp_payload` and native SKU rows retained the barcode, but
`_build_pdp_insert` omitted it. Consequently `_apply_pdp_identity_gate` could
not pass this strong evidence to the identity resolver. The planned PDP now
includes `gtin`, normalized using the same established `canonical_gtin`
utility as the apply gate (`08809530070499` for this source). No identifier is
inferred from a title, URL or seller. Missing/rejected identifiers stay null.
This reuses the existing SQL `gtin` bind and column; the input `barcode` alias
is not added to the SQL row.

The regression exercises both captured sources through mapping and planning,
then calls the apply gate with a mocked identity resolver. It proves the real
GTIN reaches that resolver, that an ATTACH result can align `content_key`,
and that seller IDs, native variant IDs, product keys and signatures survive.
It also checks the existing SQL accepts every planned row key and verifies
missing/rejected GTIN and disabled-gate behavior. The utility's established
normalization rules are unchanged; this is not new barcode validation policy.

Pure planning still produces two PDP keys, two signatures, four SKUs and four
offers (each source has a canonical SKU and one native SKU). It performs no
database identity lookup and does not merge those PDPs. Actual cross-retailer
content attachment depends on the apply identity gate being enabled, exact
matching against database state, and downstream group membership/offer serving.
The fixture's mocked ATTACH result is not evidence that production attachment
has happened. Bulk-feed barcode recovery remains a separate prerequisite for
this strong identity to be available in ordinary feed ingestion. No database
apply, production mutation or live identity lookup was run for this change.

The final observation boundary shares `catalog_identity.validated_source_gtin` across detail recovery, PDP-row handoff and acceptance screening. It rejects invalid lengths/check digits and all-zero identifiers before normalizing to GTIN-14. The legacy normalization function is unchanged.
