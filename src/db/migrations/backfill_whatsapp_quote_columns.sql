-- Backfill columns added after early WhatsApp ingest stored quote/forward
-- evidence only in the raw metadata JSON.

UPDATE whatsapp_messages
SET quoted_message_id = COALESCE(
        NULLIF(quoted_message_id, ''),
        NULLIF(COALESCE(metadata->>'quoted_message_id', metadata->>'quoted_msg_id'), '')
    ),
    quoted_text = COALESCE(
        NULLIF(quoted_text, ''),
        NULLIF(metadata->>'quoted_text', '')
    ),
    forward_from_name = COALESCE(
        NULLIF(forward_from_name, ''),
        NULLIF(COALESCE(
            metadata->>'forward_from_name',
            metadata->>'forwarded_newsletter_name',
            metadata->>'forwardFromName'
        ), '')
    )
WHERE metadata IS NOT NULL
  AND (
      ((quoted_message_id IS NULL OR quoted_message_id = '')
       AND NULLIF(COALESCE(metadata->>'quoted_message_id', metadata->>'quoted_msg_id'), '') IS NOT NULL)
      OR ((quoted_text IS NULL OR quoted_text = '')
          AND NULLIF(metadata->>'quoted_text', '') IS NOT NULL)
      OR ((forward_from_name IS NULL OR forward_from_name = '')
          AND NULLIF(COALESCE(
              metadata->>'forward_from_name',
              metadata->>'forwarded_newsletter_name',
              metadata->>'forwardFromName'
          ), '') IS NOT NULL)
  );
