from shared.observability import (
    get_logger,
    make_counter,
    make_gauge,
    make_histogram,
    start_metrics_server,
)

messages_processed = make_counter(
    "collector_messages_processed_total",
    "Total processed collector messages",
    ["queue", "status"],
)

message_latency = make_histogram(
    "collector_message_latency_seconds",
    "Collector handler latency",
    ["queue"],
)

dlq_depth = make_gauge(
    "collector_dlq_depth",
    "Depth of dlq.failed queue",
)
