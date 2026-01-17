-- Create short-link table for buyer review invitation URLs.
-- Maps a short `code` to an `invitation_token`, so emails can use short URLs.

CREATE TABLE IF NOT EXISTS reviews_invitation_shortlinks (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(32) UNIQUE NOT NULL,
  merchant_id VARCHAR(64) NOT NULL,
  order_id TEXT NOT NULL,
  invitation_token TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reviews_invitation_shortlinks_merchant_created
  ON reviews_invitation_shortlinks (merchant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reviews_invitation_shortlinks_expires_at
  ON reviews_invitation_shortlinks (expires_at);

