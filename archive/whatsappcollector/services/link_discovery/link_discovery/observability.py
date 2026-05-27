from shared.observability import get_logger, make_counter, make_gauge, start_metrics_server

links_discovered_total = make_counter(
    "link_discovery_links_discovered_total",
    "Total links discovered",
    ["link_type"],
)

links_deduplicated_total = make_counter(
    "link_discovery_links_deduplicated_total",
    "Total duplicate links skipped",
)

rules_matched_total = make_counter(
    "link_discovery_rules_matched_total",
    "Queue rules matched",
    ["rule_id"],
)

cursor_position_gauge = make_gauge(
    "link_discovery_cursor_position",
    "Current link_discovery cursor",
)
