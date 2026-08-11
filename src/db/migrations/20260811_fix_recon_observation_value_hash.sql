ALTER TABLE recon_observations
    ADD COLUMN IF NOT EXISTS value_hash TEXT;

UPDATE recon_observations
SET value_hash = encode(digest(value, 'sha256'), 'hex')
WHERE value_hash IS NULL;

ALTER TABLE recon_observations
    ALTER COLUMN value_hash SET NOT NULL;

ALTER TABLE recon_observations
    DROP CONSTRAINT IF EXISTS recon_observations_target_id_module_observation_type_value_key;

DROP INDEX IF EXISTS idx_recon_observations_type_value;

CREATE UNIQUE INDEX IF NOT EXISTS ux_recon_observations_target_module_type_value_hash
    ON recon_observations (target_id, module, observation_type, value_hash);

CREATE INDEX IF NOT EXISTS idx_recon_observations_type_value_hash
    ON recon_observations (observation_type, value_hash);
