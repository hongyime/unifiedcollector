CREATE TABLE IF NOT EXISTS whatsapp_lid_map (
    lid VARCHAR(255) PRIMARY KEY,
    phone_jid VARCHAR(255) NOT NULL,
    display_name VARCHAR(255),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wa_lid_map_phone ON whatsapp_lid_map(phone_jid);
