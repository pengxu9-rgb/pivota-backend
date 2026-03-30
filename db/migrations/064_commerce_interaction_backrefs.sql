ALTER TABLE surface_click_events
    ADD COLUMN IF NOT EXISTS interaction_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_surface_click_events_interaction
    ON surface_click_events(interaction_id);

ALTER TABLE commerce_attribution_edges
    ADD COLUMN IF NOT EXISTS interaction_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_interaction
    ON commerce_attribution_edges(interaction_id);

ALTER TABLE surface_listing_states
    ADD COLUMN IF NOT EXISTS interaction_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_surface_listing_states_interaction
    ON surface_listing_states(interaction_id);
