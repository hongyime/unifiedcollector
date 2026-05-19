import hashlib
import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_MODELS_DIR = Path(__file__).resolve().parent.parent.parent / "models" / "dlib"
DLIB_MODELS_PATH = os.getenv("DLIB_MODELS_PATH", "") or str(_REPO_MODELS_DIR)

try:
    import face_recognition
    import numpy as np
    HAS_FACE_RECOGNITION = True
except ImportError:
    HAS_FACE_RECOGNITION = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import imagehash
    from PIL import Image
    HAS_PHASH = True
except ImportError:
    HAS_PHASH = False


@dataclass
class FaceEmbedding:
    embedding: list[float]
    bbox_top: int
    bbox_right: int
    bbox_bottom: int
    bbox_left: int
    confidence: float
    frame_index: int = 0


def get_model_path(model_name: str) -> str:
    repo_path = os.path.join(DLIB_MODELS_PATH, model_name)
    if os.path.exists(repo_path):
        return repo_path
    return model_name


class FaceProcessor:

    def __init__(self, model: str = "hog", upsample: int = 1,
                 max_dimension: int = 4096, phash_threshold: int = 10):
        self._model = model
        self._upsample = upsample
        self._max_dimension = max_dimension
        self._phash_threshold = phash_threshold
        predictor = get_model_path("shape_predictor_68_face_landmarks.dat")
        resnet = get_model_path("dlib_face_recognition_resnet_model_v1.dat")
        if HAS_FACE_RECOGNITION and os.path.exists(predictor):
            logger.info("Using dlib models from %s", DLIB_MODELS_PATH)

    @property
    def available(self) -> bool:
        return HAS_FACE_RECOGNITION

    def encode_image(self, data: bytes, frame_index: int = 0) -> list[FaceEmbedding]:
        if not HAS_FACE_RECOGNITION:
            return []

        try:
            img = face_recognition.load_image_file(io.BytesIO(data))

            h, w = img.shape[:2]
            if max(h, w) > self._max_dimension:
                scale = self._max_dimension / max(h, w)
                new_w, new_h = int(w * scale), int(h * scale)
                img = cv2.resize(img, (new_w, new_h)) if HAS_CV2 else img

            locations = face_recognition.face_locations(img, number_of_times_to_upsample=self._upsample, model=self._model)
            encodings = face_recognition.face_encodings(img, known_face_locations=locations)

            results = []
            for (top, right, bottom, left), encoding in zip(locations, encodings):
                results.append(FaceEmbedding(
                    embedding=encoding.tolist(),
                    bbox_top=top,
                    bbox_right=right,
                    bbox_bottom=bottom,
                    bbox_left=left,
                    confidence=1.0,
                    frame_index=frame_index,
                ))
            return results
        except Exception as e:
            logger.debug("Face encoding failed: %s", e)
            return []

    def extract_video_frames(self, video_path: str, fps: int = 1) -> list[tuple[bytes, int]]:
        if not HAS_CV2:
            return []

        frames: list[tuple[bytes, int]] = []
        seen_hashes: set[str] = set()

        try:
            cap = cv2.VideoCapture(video_path)
            video_fps = cap.get(cv2.CAP_PROP_FPS) or 30
            frame_interval = max(1, int(video_fps / fps))
            frame_idx = 0

            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                if frame_idx % frame_interval == 0:
                    _, buf = cv2.imencode(".jpg", frame)
                    frame_bytes = buf.tobytes()

                    if HAS_PHASH:
                        ph = str(imagehash.phash(Image.open(io.BytesIO(frame_bytes))))
                        if self._is_duplicate_frame(ph, seen_hashes):
                            frame_idx += 1
                            continue
                        seen_hashes.add(ph)

                    frames.append((frame_bytes, frame_idx))

                frame_idx += 1

            cap.release()
        except Exception as e:
            logger.debug("Video frame extraction failed: %s", e)

        return frames

    def _is_duplicate_frame(self, phash_str: str, seen: set[str]) -> bool:
        if not HAS_PHASH:
            return False
        for existing in seen:
            try:
                h1 = imagehash.hex_to_hash(phash_str)
                h2 = imagehash.hex_to_hash(existing)
                if h1 - h2 <= self._phash_threshold:
                    return True
            except Exception:
                continue
        return False

    def encode_video(self, video_path: str, fps: int = 1) -> list[FaceEmbedding]:
        frames = self.extract_video_frames(video_path, fps)
        all_embeddings: list[FaceEmbedding] = []
        for frame_bytes, frame_idx in frames:
            embeddings = self.encode_image(frame_bytes, frame_index=frame_idx)
            all_embeddings.extend(embeddings)
        return all_embeddings
