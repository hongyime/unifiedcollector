from shared.observability import get_logger, make_counter, make_gauge, start_metrics_server

jobs_started_total = make_counter("bulk_sender_jobs_started_total", "Total bulk sender jobs started", ["mode"])
sends_attempted_total = make_counter("bulk_sender_sends_attempted_total", "Total send attempts", ["mode", "status"])
job_status_gauge = make_gauge("bulk_sender_active_jobs", "Active running jobs")
