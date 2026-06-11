-- Fix phone_number on existing @lid rows: clear bogus LID numbers that were
-- extracted by the old \d{7,15} regex (which matched LID prefixes like 126113225400357).
-- Only @s.whatsapp.net JIDs should have phone_number populated.
UPDATE whatsapp_users
SET phone_number = NULL
WHERE platform_user_id LIKE '%@lid'
  AND phone_number IS NOT NULL;

-- Once whatsapp_lid_map is populated (via contacts.update events over time),
-- run this to migrate existing @lid rows to phone-based JIDs:
-- UPDATE whatsapp_users wu
-- SET platform_user_id = lm.phone_jid,
--     phone_number = split_part(lm.phone_jid, '@', 1)
-- FROM whatsapp_lid_map lm
-- WHERE wu.platform_user_id = lm.lid
--   AND NOT EXISTS (
--     SELECT 1 FROM whatsapp_users WHERE platform_user_id = lm.phone_jid
--   );
