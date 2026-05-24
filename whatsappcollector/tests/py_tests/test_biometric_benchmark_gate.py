import os

import numpy as np


def _l2_nearest(probe: np.ndarray, gallery: np.ndarray) -> tuple[int, float]:
    distances = np.linalg.norm(gallery - probe, axis=1)
    nearest_index = int(np.argmin(distances))
    return nearest_index, float(distances[nearest_index])


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.clip(norms, 1e-12, None)


def test_biometric_benchmark_threshold_gate():
    """Synthetic benchmark gate for biometric threshold quality.

    This enforces two measurable guardrails for FACE_MATCH_THRESHOLD:
    - Detection rate (true positives) must stay high
    - False-positive rate must stay low
    """
    rng = np.random.default_rng(20250113)

    dimensions = 128
    identity_count = 24
    probes_per_identity = 12
    impostor_count = 500

    threshold = float(os.getenv("FACE_MATCH_THRESHOLD", "0.6"))
    min_detection_rate = float(os.getenv("BIOMETRIC_MIN_DETECTION_RATE", "0.99"))
    max_false_positive_rate = float(os.getenv("BIOMETRIC_MAX_FALSE_POSITIVE_RATE", "0.01"))

    centroids = _normalize(rng.normal(size=(identity_count, dimensions)).astype(np.float32))

    true_positives = 0
    positive_total = identity_count * probes_per_identity

    for identity_index, centroid in enumerate(centroids):
        probes = centroid + rng.normal(0.0, 0.02, size=(probes_per_identity, dimensions)).astype(np.float32)
        probes = _normalize(probes)

        for probe in probes:
            nearest_index, nearest_distance = _l2_nearest(probe, centroids)
            if nearest_index == identity_index and nearest_distance <= threshold:
                true_positives += 1

    detection_rate = true_positives / positive_total

    false_positives = 0
    impostor_probes = _normalize(rng.normal(size=(impostor_count, dimensions)).astype(np.float32))

    for probe in impostor_probes:
        _, nearest_distance = _l2_nearest(probe, centroids)
        if nearest_distance <= threshold:
            false_positives += 1

    false_positive_rate = false_positives / impostor_count

    assert detection_rate >= min_detection_rate, (
        f"Detection rate {detection_rate:.4f} below gate {min_detection_rate:.4f} "
        f"at threshold {threshold:.4f}."
    )
    assert false_positive_rate <= max_false_positive_rate, (
        f"False-positive rate {false_positive_rate:.4f} above gate {max_false_positive_rate:.4f} "
        f"at threshold {threshold:.4f}."
    )
