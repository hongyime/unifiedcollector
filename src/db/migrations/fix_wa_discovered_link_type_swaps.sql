-- Repair historical WhatsApp link rows written with url/link_type swapped.
-- Safe shape: url=<actual URL>, link_type=<coarse type>.

UPDATE wa_discovered_links
SET url = link_type,
    link_type = 'url',
    domain = COALESCE(NULLIF(domain, ''), lower(split_part(split_part(link_type, '/', 3), ':', 1)))
WHERE url = 'url'
  AND link_type ~* '^https?://';
