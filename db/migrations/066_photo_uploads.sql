CREATE TABLE IF NOT EXISTS photo_uploads (
  upload_id TEXT PRIMARY KEY,
  agent_id TEXT NULL,
  user_id TEXT NULL,
  consented BOOLEAN NOT NULL DEFAULT false,
  consented_at TIMESTAMP NULL,
  status TEXT NOT NULL DEFAULT 'created',
  qc_status TEXT NULL,
  qc_advice TEXT NULL,
  qc_details TEXT NULL,
  bucket TEXT NULL,
  object_key TEXT NULL,
  content_type TEXT NULL,
  byte_size INTEGER NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP NULL,
  deleted_at TIMESTAMP NULL
);

CREATE INDEX IF NOT EXISTS idx_photo_uploads_expires_at ON photo_uploads(expires_at);
CREATE INDEX IF NOT EXISTS idx_photo_uploads_status ON photo_uploads(status);
