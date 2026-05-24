from __future__ import annotations

from shared.observability import get_logger, make_counter, make_gauge, make_histogram, start_metrics_server

faces_processed_total = make_counter("face_recognition_faces_processed_total", "Faces processed")
face_embeddings_total = make_counter("face_recognition_face_embeddings_total", "Face embeddings stored")
identity_matches_total = make_counter(
    "face_recognition_identity_matches_total",
    "Identity matcher outcomes",
    ["result"],
)
findings_queued_total = make_counter("face_recognition_findings_queued_total", "Findings queued")
findings_published_total = make_counter("face_recognition_findings_published_total", "Findings published")
findings_skipped_total = make_counter(
    "face_recognition_findings_skipped_total",
    "Findings skipped by confidence gate",
)
face_processing_seconds = make_histogram(
    "face_recognition_processing_seconds",
    "Face processing latency",
)
publisher_queue_depth = make_gauge("face_recognition_publisher_queue_depth", "Queued findings")
