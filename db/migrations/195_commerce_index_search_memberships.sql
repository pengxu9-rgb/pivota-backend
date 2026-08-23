-- Preserve only the source-ref membership of a published serving document.
-- This lets a targeted worker repair both sides when identity resolution moves
-- a product from one sellable-item group to another.

CREATE TABLE IF NOT EXISTS commerce_index_search_memberships (
    source_ref VARCHAR(255) PRIMARY KEY,
    document_id VARCHAR(255) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_commerce_index_search_memberships_document
    ON commerce_index_search_memberships (document_id);
