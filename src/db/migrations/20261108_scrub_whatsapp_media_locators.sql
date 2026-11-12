-- Session-bound media keys/paths belong only in the paired bridge's memory.
UPDATE whatsapp_messages
SET metadata = COALESCE(metadata, '{}'::jsonb)
    - 'mediaKey' - 'directPath' - 'media_url' - 'url'
WHERE COALESCE(metadata, '{}'::jsonb) ?| ARRAY['mediaKey', 'directPath', 'media_url', 'url'];
