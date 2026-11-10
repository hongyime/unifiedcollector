-- The retired Instagram-DM collector must not survive in restored health ledgers.
DELETE FROM source_health WHERE source = 'instagram_dm';
