-- WhatsApp device intelligence (read-only device sweep). Populated by
-- src/core/wa_device_sweep.py via the bridge /devices endpoint (getUSyncDevices
-- + onWhatsApp — server queries only, no messages/reactions/calls sent).
-- Analyzer reads these cross-DB for the person-page Device Intelligence panel.

CREATE TABLE IF NOT EXISTS wa_devices (
    phone_jid     TEXT NOT NULL,            -- "<digits>@s.whatsapp.net"
    device_id     INTEGER NOT NULL,         -- 0 = primary/phone, >0 = companion (web/desktop)
    exists_on_wa  BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (phone_jid, device_id)
);

CREATE TABLE IF NOT EXISTS wa_device_observations (
    id            BIGSERIAL PRIMARY KEY,
    phone_jid     TEXT NOT NULL,
    probed_by     TEXT NOT NULL,            -- session_1 / session_2 (which account issued the query)
    exists_on_wa  BOOLEAN NOT NULL,
    device_count  INTEGER NOT NULL,
    device_ids    INTEGER[] NOT NULL DEFAULT '{}',
    observed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wa_dev_obs ON wa_device_observations (phone_jid, observed_at DESC);
