-- Drops the Shop account-link storage (migration 277). Turn the SHOP_MCP_* flags off first. Before
-- dropping shop_account_links, revoke every live refresh token at Shop (POST /oauth/revoke): dropping
-- the rows loses the only copy, but the tokens stay valid at Shop until revoked or expired.
DROP TABLE IF EXISTS shop_checkouts;
DROP TABLE IF EXISTS shop_link_attempts;
DROP TABLE IF EXISTS shop_account_links;
DROP TABLE IF EXISTS shop_account_claims;
