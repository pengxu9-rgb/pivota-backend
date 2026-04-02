ALTER TABLE merchant_portal_preferences
ADD COLUMN IF NOT EXISTS portal_language VARCHAR(16) NOT NULL DEFAULT 'en';
