from shared.observability import get_logger, make_counter, make_gauge, make_histogram, start_metrics_server

media_downloads_total = make_counter(
    "media_archival_downloads_total",
    "Media files downloaded or reused from disk",
    ["status", "mime_type"],
)

cleanup_deletions_total = make_counter(
    "media_archival_cleanup_deletions_total",
    "Media files deleted during cleanup",
    ["kind"],
)

cleanup_duration_seconds = make_histogram(
    "media_archival_cleanup_duration_seconds",
    "Duration of cleanup runs",
)

queue_depth_gauge = make_gauge(
    "media_archival_queue_depth",
    "Depth of the raw media backlog",
)
