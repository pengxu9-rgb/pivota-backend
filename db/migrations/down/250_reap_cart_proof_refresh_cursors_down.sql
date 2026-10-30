-- Reverses 250_reap_cart_proof_refresh_cursors.sql. The only reader and writer is jobs/reap_cart_proof_refresh.py;
-- without the table every store restarts at its first page and the mirror lane treats every store as never
-- completed (it orders them by proof age and name). Nothing fails.
--
-- NOT STICKY ON ITS OWN: db/reap_cart_proof_refresh_cursors.ensure_table() recreates it on the next apply run.
DROP TABLE IF EXISTS reap_cart_proof_refresh_cursors;
