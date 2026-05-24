from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import settings
from .observability import face_processing_seconds, faces_processed_total, get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class FaceEmbedding:
    embedding: list[float]
    bbox: tuple[int, int, int, int]
    confidence: float
    frame_index: int = 0
    source_path: str | None = None


def _float_list(values: Any) -> list[float]:
    if hasattr(values, "tolist"):
        values = values.tolist()
    return [float(value) for value in values]


class FaceProcessor:
    def __init__(self) -> None:
        self.model_path = Path(settings.FACE_MODELS_PATH)
        self.max_dimension = settings.MAX_IMAGE_DIMENSION
        self.detection_model = settings.FACE_DETECTION_MODEL
        self.upsample_times = settings.FACE_UPSAMPLE_TIMES
        self.predictor_path = self.model_path / "shape_predictor_68_face_landmarks.dat"
        self.resnet_path = self.model_path / "dlib_face_recognition_resnet_model_v1.dat"
        self.models_ready = False
        self._verify_models()

    def _verify_models(self) -> None:
        if not self.predictor_path.exists() or not self.resnet_path.exists():
            self.models_ready = False
            logger.warning(
                "face_models_missing",
                predictor_exists=self.predictor_path.exists(),
                resnet_exists=self.resnet_path.exists(),
                models_path=str(self.model_path),
            )
            return

        self.models_ready = True

    def encode_image(self, image_path: str, frame_index: int = 0) -> list[FaceEmbedding]:
        try:
            import cv2
            import face_recognition
        except ImportError as exc:  # pragma: no cover - runtime dependency guard
            raise RuntimeError("face recognition dependencies are not installed") from exc

        results: list[FaceEmbedding] = []
        if not os.path.exists(image_path):
            logger.warning("face_image_missing", image_path=image_path)
            return results

        try:
            image = cv2.imread(image_path)
            if image is None:
                logger.warning("face_image_decode_failed", image_path=image_path)
                return results

            rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            height, width = rgb_image.shape[:2]
            if max(height, width) > self.max_dimension:
                scale = self.max_dimension / float(max(height, width))
                new_width = max(1, int(width * scale))
                new_height = max(1, int(height * scale))
                rgb_image = cv2.resize(rgb_image, (new_width, new_height), interpolation=cv2.INTER_AREA)

            start = time.perf_counter()
            face_locations = face_recognition.face_locations(
                rgb_image,
                model=self.detection_model,
                number_of_times_to_upsample=self.upsample_times,
            )
            if not face_locations:
                logger.debug("face_no_faces_detected", image_path=image_path)
                return results

            encodings = face_recognition.face_encodings(
                rgb_image,
                known_face_locations=face_locations,
                num_jitters=1,
            )
            for index, encoding in enumerate(encodings):
                embedding = _float_list(encoding)
                if len(embedding) != 128:
                    logger.warning("face_invalid_embedding_length", image_path=image_path, length=len(embedding))
                    continue
                bbox = tuple(int(value) for value in face_locations[index])
                results.append(
                    FaceEmbedding(
                        embedding=embedding,
                        bbox=bbox,  # type: ignore[arg-type]
                        confidence=1.0,
                        frame_index=frame_index,
                        source_path=image_path,
                    )
                )

            faces_processed_total.inc(len(results))
            face_processing_seconds.observe(time.perf_counter() - start)
            logger.info("face_image_processed", image_path=image_path, faces=len(results))
        except Exception as exc:  # pragma: no cover - defensive runtime logging
            logger.error("face_image_processing_failed", image_path=image_path, error=str(exc), exc_info=True)

        return results

    def extract_video_frames(self, video_path: str, message_type: str | None = None) -> list[str]:
        try:
            import cv2
            import imagehash
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - runtime dependency guard
            raise RuntimeError("video extraction dependencies are not installed") from exc

        extracted_frames: list[str] = []
        if not os.path.exists(video_path):
            logger.warning("face_video_missing", video_path=video_path)
            return extracted_frames

        target_rate = settings.VIDEO_NOTE_FRAME_RATE if message_type == "video_note" else settings.VIDEO_FRAME_RATE
        video_hash = hashlib.md5(video_path.encode("utf-8")).hexdigest()
        temp_dir = Path(tempfile.gettempdir()) / "wac_face" / video_hash
        temp_dir.mkdir(parents=True, exist_ok=True)

        capture = cv2.VideoCapture(video_path)
        if not capture.isOpened():
            logger.warning("face_video_open_failed", video_path=video_path)
            return extracted_frames

        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0 or frame_count <= 0:
            logger.warning("face_video_invalid_metadata", video_path=video_path, fps=fps, frame_count=frame_count)
            capture.release()
            return extracted_frames

        frame_interval = max(1, int(round(fps / max(target_rate, 0.1))))
        last_hash = None
        accepted_count = 0
        frame_index = 0

        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break

                if frame_index % frame_interval == 0:
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    image = Image.fromarray(rgb_frame)
                    if max(image.width, image.height) > self.max_dimension:
                        image.thumbnail((self.max_dimension, self.max_dimension), Image.Resampling.LANCZOS)

                    current_hash = imagehash.phash(image)
                    if last_hash is not None and (current_hash - last_hash) <= settings.PHASH_DEDUP_THRESHOLD:
                        frame_index += 1
                        continue

                    last_hash = current_hash
                    frame_path = temp_dir / f"frame_{accepted_count:06d}.jpg"
                    image.save(frame_path, format="JPEG", quality=90)
                    extracted_frames.append(str(frame_path))
                    accepted_count += 1

                frame_index += 1
        finally:
            capture.release()

        logger.info("face_video_frames_extracted", video_path=video_path, extracted=accepted_count)
        return extracted_frames

    def process_media_file(self, file_path: str, mime_type: str | None, message_type: str | None = None) -> list[FaceEmbedding]:
        if not self.models_ready:
            return []

        if not file_path or not os.path.exists(file_path):
            logger.warning("face_media_file_missing", file_path=file_path)
            return []

        mime_type = (mime_type or "").lower()
        if mime_type.startswith("image/"):
            return self.encode_image(file_path, frame_index=0)

        if mime_type.startswith("video/"):
            embeddings: list[FaceEmbedding] = []
            frame_paths = self.extract_video_frames(file_path, message_type=message_type)
            try:
                for frame_index, frame_path in enumerate(frame_paths):
                    embeddings.extend(self.encode_image(frame_path, frame_index=frame_index))
            finally:
                for frame_path in frame_paths:
                    try:
                        os.unlink(frame_path)
                    except OSError:
                        pass
            return embeddings

        logger.debug("face_media_type_skipped", file_path=file_path, mime_type=mime_type)
        return []


face_processor = FaceProcessor()
