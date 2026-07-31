-- Dashboard source-matrix Beeper sub-source liveness groups recent messages by
-- network. The existing ingested_at-only index finds the rows but still forces
-- heap reads to fetch network on large Beeper histories.
CREATE INDEX IF NOT EXISTS idx_beeper_shadow_ingested_network
    ON beeper_shadow_messages (ingested_at DESC, network);

-- The same matrix groups participant coverage by network and distinct
-- participant_id. Cover both columns to avoid cold heap-heavy scans.
CREATE INDEX IF NOT EXISTS idx_beeper_shadow_participants_network_participant
    ON beeper_shadow_participants (network, participant_id);
