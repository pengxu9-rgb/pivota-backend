-- Migration 028: Platform Import Reports
-- Purpose: Store raw platform CSV/Excel reports for import tasks

CREATE TABLE IF NOT EXISTS platform_import_reports (
    id SERIAL PRIMARY KEY,

    -- Ownership
    merchant_id VARCHAR(50) NOT NULL,
    report_type VARCHAR(50) NOT NULL, -- 'amazon' | 'temu' | ...

    -- File metadata
    original_filename VARCHAR(255),
    file_size_bytes INTEGER,
    rows_total INTEGER,

    -- Raw content (Phase 1: inline storage; Phase 2+ may move to object storage)
    raw_content TEXT,
    storage_type VARCHAR(20) DEFAULT 'inline', -- 'inline' | 's3'
    s3_key VARCHAR(500),

    -- Import linkage
    import_task_id INTEGER,

    -- Audit
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_by VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_platform_import_reports_merchant_created
    ON platform_import_reports(merchant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_platform_import_reports_import_task
    ON platform_import_reports(import_task_id);

