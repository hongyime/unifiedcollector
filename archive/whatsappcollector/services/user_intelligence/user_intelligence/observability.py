from shared.observability import get_logger, make_counter, make_gauge, start_metrics_server

sightings_processed_total = make_counter(
    "user_intelligence_sightings_processed_total",
    "Total user sightings processed",
)

history_changes_total = make_counter(
    "user_intelligence_history_changes_total",
    "Total user history field changes",
    ["field_name"],
)

memberships_upsert_total = make_counter(
    "user_intelligence_memberships_upsert_total",
    "Total membership upserts",
)

connections_updates_total = make_counter(
    "user_intelligence_connections_updates_total",
    "Total user connection updates",
)

cursor_position_gauge = make_gauge(
    "user_intelligence_cursor_position",
    "Current user_intelligence cursor",
)
