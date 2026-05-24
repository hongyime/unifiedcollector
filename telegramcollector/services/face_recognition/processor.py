"""
Face Processor — services/face_recognition/processor.py

Handles face detection and embedding extraction using InsightFace buffalo_l.
Runs detection in a thread pool to avoid blocking the asyncio event loop.
"""
import logging
import asyncio
import threading
import numpy as np
import cv2
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar, List, Dict, Optional

warnings.filterwarnings("ignore", category=FutureWarning, module="insightface")
warnings.filterwarnings("ignore", category=FutureWarning, module="skimage")

import io
import time
from PIL import Image
from shared.config import settings, get_dynamic_setting

logger = logging.getLogger(__name__)


class FaceProcessor:
    """
    Handles face detection and embedding extraction using InsightFace buffalo_l.
    Singleton — use get_instance() to obtain the shared instance.
    """

    _instance: ClassVar[Optional['FaceProcessor']] = None
    _executor: ClassVar[ThreadPoolExecutor] = ThreadPoolExecutor(max_workers=2)
    _initialized: ClassVar[bool] = False
    _init_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, providers: list[str] | None = None) -> None:
        """
        providers: ONNX Runtime execution providers.
                   None → auto-detect from USE_GPU setting.
                   GPU path: ['CUDAExecutionProvider', 'CPUExecutionProvider']
                   CPU path: ['CPUExecutionProvider']
        """
        self.providers = providers
        self.app = None
        self.min_quality = 0.3  # placeholder; overwritten during _lazy_init

    # ------------------------------------------------------------------
    # Singleton helpers
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> 'FaceProcessor':
        """Returns the singleton, calling _lazy_init() if needed."""
        if cls._instance is None:
            cls._instance = FaceProcessor()
        cls._instance._lazy_init()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Resets singleton (used in tests)."""
        if cls._instance is not None:
            cls._instance = None
            cls._initialized = False
            logger.info("FaceProcessor instance reset")

    # ------------------------------------------------------------------
    # Lazy initialisation
    # ------------------------------------------------------------------

    def _lazy_init(self) -> bool:
        """Lazy initialisation of InsightFace model with retry logic (thread-safe)."""
        if self._initialized:
            return True

        with self._init_lock:
            if self._initialized:
                return True

            MAX_RETRIES = 3

            for attempt in range(MAX_RETRIES):
                try:
                    from insightface.app import FaceAnalysis

                    if self.providers is None:
                        if settings.USE_GPU:
                            self.providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
                        else:
                            self.providers = ['CPUExecutionProvider']

                    self.app = FaceAnalysis(name='buffalo_l', providers=self.providers)
                    self.app.prepare(ctx_id=0, det_size=(640, 640))

                    self.min_quality = settings.FACE_MIN_QUALITY_THRESHOLD

                    FaceProcessor._initialized = True
                    logger.info(f"InsightFace initialised successfully. Providers: {self.providers}")
                    return True

                except Exception as e:
                    logger.error(
                        f"FaceProcessor init failed with {self.providers} "
                        f"(attempt {attempt + 1}/{MAX_RETRIES}): {e}"
                    )

                    # Requirement 5.3 — GPU failure → fall back to CPU immediately
                    if self.providers and 'CUDAExecutionProvider' in self.providers:
                        logger.warning("GPU initialisation failed. Falling back to CPU...")
                        self.providers = ['CPUExecutionProvider']
                        continue

                    if attempt < MAX_RETRIES - 1:
                        time.sleep(1)
                    else:
                        logger.critical("FaceProcessor initialisation failed after max retries")
                        return False

        return False

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def process_message(self, message: dict) -> list[dict]:
        """
        Top-level entry point for one raw_messages row.

        message keys used: message_type, media_path, file_unique_id

        Returns list of face dicts:
          { embedding: list[float],  # 512-dim normalised
            bbox: list[float],       # [x1, y1, x2, y2]
            quality: float,          # det_score
            frame_index: int,
            landmarks: list | None }

        Raises FileNotFoundError if media_path does not exist.
        Raises ValueError if file is corrupt/unreadable.
        """
        media_path: str = message.get('media_path', '')
        message_type: str = message.get('message_type', '')

        frames = await self.extract_frames(media_path, message_type)
        return await self._process_frames(frames)

    async def extract_frames(self, media_path: str, message_type: str) -> list[np.ndarray]:
        """
        Dispatches to the correct extraction strategy based on message_type.

        Returns list of BGR numpy arrays.
        Raises FileNotFoundError / ValueError on I/O errors.
        """
        import os
        if not os.path.exists(media_path):
            raise FileNotFoundError(f"Media file not found: {media_path}")

        if message_type == 'photo':
            frame = cv2.imread(media_path)
            if frame is None:
                raise ValueError(f"Cannot read image file: {media_path}")
            return [frame]

        elif message_type == 'video':
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor, self._extract_video_frames, media_path
            )

        elif message_type == 'circle_video':
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor, self._extract_circle_video_frames, media_path
            )

        else:
            logger.warning(f"Unknown message_type '{message_type}' — treating as photo")
            frame = cv2.imread(media_path)
            if frame is None:
                raise ValueError(f"Cannot read file: {media_path}")
            return [frame]

    async def process_image(self, image_input) -> List[Dict]:
        """
        Detects faces in a single image input.

        image_input: numpy array (BGR), PIL Image, or BytesIO/bytes buffer.
        Returns list of face dicts.
        """
        image_array = self._to_numpy(image_input)
        if image_array is None:
            return []

        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor, self._detect_faces_sync, image_array
            )
        except (MemoryError, RuntimeError) as e:
            logger.error(f"Face processing failed (resource error): {e}")
            return []
        except Exception as e:
            logger.error(f"Face processing unexpected error: {e}")
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _process_frames(self, frames: list[np.ndarray]) -> list[dict]:
        """Runs detection on each frame and aggregates results with frame_index."""
        all_faces: list[dict] = []
        loop = asyncio.get_running_loop()

        for i, frame in enumerate(frames):
            faces = await loop.run_in_executor(
                self._executor, self._detect_faces_sync, frame
            )
            for face in faces:
                face['frame_index'] = i
            all_faces.extend(faces)

        logger.debug(f"Processed {len(frames)} frames, found {len(all_faces)} faces")
        return all_faces

    def _detect_faces_sync(self, image_array: np.ndarray) -> list[dict]:
        """
        Synchronous InsightFace detection (runs in thread pool via run_in_executor).
        Applies quality filter (>= FACE_MIN_QUALITY_THRESHOLD) and size filter (>= 40×40).
        Returns filtered list of face dicts.
        """
        if not self._lazy_init():
            return []

        try:
            faces = self.app.get(image_array)
            results = []

            dynamic_quality = get_dynamic_setting(
                "FACE_MIN_QUALITY_THRESHOLD", settings.FACE_MIN_QUALITY_THRESHOLD
            )

            for face in faces:
                quality = float(face.det_score)
                if quality < dynamic_quality:
                    logger.debug(f"Skipping low-quality face: {quality:.3f} < {dynamic_quality}")
                    continue

                bbox = face.bbox
                width = bbox[2] - bbox[0]
                height = bbox[3] - bbox[1]
                if width < 40 or height < 40:
                    logger.debug(f"Skipping tiny face: {width:.1f}×{height:.1f}")
                    continue

                face_dict: dict = {
                    'embedding': face.normed_embedding.tolist(),
                    'bbox': bbox.tolist(),
                    'quality': quality,
                    'landmarks': None,
                }

                if hasattr(face, 'landmark_2d_106') and face.landmark_2d_106 is not None:
                    face_dict['landmarks'] = face.landmark_2d_106.tolist()
                elif hasattr(face, 'kps') and face.kps is not None:
                    face_dict['landmarks'] = face.kps.tolist()

                results.append(face_dict)

            logger.debug(f"Detected {len(results)} faces (filtered from {len(faces)})")
            return results

        except Exception as e:
            logger.error(f"Face detection failed: {e}")
            return []

    def _extract_video_frames(self, media_path: str) -> list[np.ndarray]:
        """
        Extracts up to FACE_VIDEO_MAX_FRAMES frames from a video file.
        Uses adaptive strategy: evenly distributes frame positions across video duration.
        """
        max_frames: int = settings.FACE_VIDEO_MAX_FRAMES
        cap = cv2.VideoCapture(media_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video file: {media_path}")

        try:
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total_frames <= 0:
                return []

            n = min(max_frames, total_frames)
            if n <= 1:
                positions = [0]
            else:
                positions = [int(i * (total_frames - 1) / (n - 1)) for i in range(n)]

            frames: list[np.ndarray] = []
            for pos in positions:
                cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
                ret, frame = cap.read()
                if ret and frame is not None:
                    frames.append(frame)

            return frames
        finally:
            cap.release()

    def _extract_circle_video_frames(self, media_path: str) -> list[np.ndarray]:
        """
        Extracts frames at FACE_CIRCLE_VIDEO_FPS fps for the full video duration.
        """
        fps_target: float = settings.FACE_CIRCLE_VIDEO_FPS
        cap = cv2.VideoCapture(media_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open circle_video file: {media_path}")

        try:
            source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total_frames <= 0 or source_fps <= 0:
                return []

            duration = total_frames / source_fps
            frame_interval = source_fps / fps_target  # source frames between samples

            positions: list[int] = []
            pos = 0.0
            while pos < total_frames:
                positions.append(int(pos))
                pos += frame_interval

            frames: list[np.ndarray] = []
            for p in positions:
                cap.set(cv2.CAP_PROP_POS_FRAMES, p)
                ret, frame = cap.read()
                if ret and frame is not None:
                    frames.append(frame)

            return frames
        finally:
            cap.release()

    # ------------------------------------------------------------------
    # Utility helpers (kept for compatibility)
    # ------------------------------------------------------------------

    def _to_numpy(self, image_input) -> Optional[np.ndarray]:
        """Converts various image formats to numpy array (BGR)."""
        try:
            if isinstance(image_input, np.ndarray):
                if len(image_input.shape) == 3 and image_input.shape[2] == 3:
                    return image_input
                return None
            elif isinstance(image_input, Image.Image):
                rgb_array = np.array(image_input)
                return cv2.cvtColor(rgb_array, cv2.COLOR_RGB2BGR)
            elif isinstance(image_input, (io.BytesIO, bytes)):
                if isinstance(image_input, bytes):
                    image_input = io.BytesIO(image_input)
                image_input.seek(0)
                pil_image = Image.open(image_input)
                rgb_array = np.array(pil_image.convert('RGB'))
                return cv2.cvtColor(rgb_array, cv2.COLOR_RGB2BGR)
            else:
                logger.warning(f"Unsupported image type: {type(image_input)}")
                return None
        except Exception as e:
            logger.error(f"Failed to convert image to numpy: {e}")
            return None

    def get_embedding_vector(self, face_dict: Dict) -> List[float]:
        """Extracts the embedding vector from a face dictionary."""
        return face_dict.get('embedding', [])

    def calculate_similarity(self, embedding1: List[float], embedding2: List[float]) -> float:
        """Calculates cosine similarity between two embeddings (0–1)."""
        vec1 = np.array(embedding1)
        vec2 = np.array(embedding2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return float(np.dot(vec1, vec2) / (norm1 * norm2))
