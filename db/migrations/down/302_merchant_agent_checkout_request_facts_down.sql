-- Reverses 302. DESTROYS every request-time door-read fact. Nothing recreates the table at the next
-- start (it has no startup heal): after this, every request-time write is a no-op until 302 is
-- applied again. The census facts (migration 294) and the run condition are untouched.
DROP TABLE IF EXISTS merchant_agent_checkout_request_facts;
