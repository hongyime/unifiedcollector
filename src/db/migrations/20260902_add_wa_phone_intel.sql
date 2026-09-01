-- WhatsApp phone-number OSINT enrichment (offline, phonenumbers library only).
-- Populated by src/core/wa_phone_intel.py against numbers already in
-- whatsapp_lid_map. Pure metadata: carrier/region/line-type/timezone from the
-- offline phonenumbers dataset — NO network calls, NO paid API, NO Google/etc.
--
-- ENRICHMENT-ONLY. A shared carrier or region does NOT link distinct people.
-- These rows are NEVER written to identity_signals and NEVER consumed by the
-- unifiedanalyzer merge scorer. Same principle as wa_devices (device-intel).
-- Analyzer reads this table cross-DB purely to decorate the person-page phone
-- panel (country flag, carrier chip, line-type badge).

CREATE TABLE IF NOT EXISTS wa_phone_intel (
    phone_jid    TEXT PRIMARY KEY,          -- "<digits>@s.whatsapp.net" (matches whatsapp_lid_map.phone_jid)
    e164         TEXT,                      -- normalized "+6591234567" (NULL if unparseable)
    country_code INTEGER,                   -- ITU numeric CC, e.g. 65, 60, 1
    region       TEXT,                      -- ISO-2 region code, e.g. "SG", "MY", "US"
    region_name  TEXT,                      -- human name from geocoder, e.g. "Singapore"
    carrier      TEXT,                      -- carrier string from phonenumbers.carrier (may be "")
    line_type    TEXT,                      -- MOBILE / FIXED_LINE / VOIP / UNKNOWN / ...
    timezones    TEXT[] NOT NULL DEFAULT '{}',  -- e.g. {"Asia/Singapore"}
    is_valid     BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wa_phone_intel_region ON wa_phone_intel (region);
CREATE INDEX IF NOT EXISTS idx_wa_phone_intel_line_type ON wa_phone_intel (line_type);
