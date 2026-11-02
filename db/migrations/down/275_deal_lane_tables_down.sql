-- Drops the deal lane's storage (migration 275). Every reader and writer is flag-gated and fails closed
-- without its table; turn the deal-lane flags off first. Dropping deletes every observed deal, its
-- verifications, the recorded program permissions and the stop history; export them first if they matter.
DROP TABLE IF EXISTS deal_network_links;
DROP TABLE IF EXISTS observed_store_offer_verifications;
DROP TABLE IF EXISTS observed_store_offers;
DROP TABLE IF EXISTS deal_program_permission_hosts;
DROP TABLE IF EXISTS deal_program_permissions;
DROP TABLE IF EXISTS deal_verify_host_daily_carts;
DROP TABLE IF EXISTS deal_lane_stops;
DROP TABLE IF EXISTS deal_lane_optout;
