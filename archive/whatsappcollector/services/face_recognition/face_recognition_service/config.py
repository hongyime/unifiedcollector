from __future__ import annotations

from pydantic import Field

from shared.config import BaseConfig


class Settings(BaseConfig):
    """Face recognition service configuration."""

    SERVICE_NAME: str = "face_recognition"

    # Runtime
    METRICS_PORT: int = 9092
    DASHBOARD_PORT: int = 8501
    LOG_LEVEL: str = "INFO"

    # Storage / models
    MEDIA_ROOT: str = "/data/media"
    FACE_MODELS_PATH: str = "/data/models"

    # Processing
    FACE_BIOMETRIC_SEMAPHORE: int = 1
    FACE_MATCH_THRESHOLD: float = 0.6
    FACE_UPSAMPLE_TIMES: int = 1
    FACE_DETECTION_MODEL: str = "hog"
    FACE_PROCESSING_BATCH_SIZE: int = 8
    FACE_POLL_SECONDS: int = 15
    MAX_IMAGE_DIMENSION: int = 1600
    VIDEO_FRAME_RATE: int = 1
    VIDEO_NOTE_FRAME_RATE: int = 2
    PHASH_DEDUP_THRESHOLD: int = 10

    # Findings publication
    FINDINGS_MAX_PER_HOUR: int = 30
    FINDINGS_SEND_DELAY: float = 3.0
    FINDINGS_MIN_CONFIDENCE: float = 0.5
    FINDINGS_QUEUE_NAME: str = "findings.publish"

    # Cursor bridge
    SERVICE_CURSOR_NAME: str = "face_recognition"

    @property
    def predictor_path(self) -> str:
        return f"{self.FACE_MODELS_PATH.rstrip('/')}/shape_predictor_68_face_landmarks.dat"

    @property
    def resnet_path(self) -> str:
        return f"{self.FACE_MODELS_PATH.rstrip('/')}/dlib_face_recognition_resnet_model_v1.dat"


settings = Settings()
