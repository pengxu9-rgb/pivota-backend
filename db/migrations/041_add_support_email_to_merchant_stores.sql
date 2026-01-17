-- Add support_email to merchant_stores for per-merchant review invitation support contact.
ALTER TABLE merchant_stores
ADD COLUMN IF NOT EXISTS support_email VARCHAR(255);
