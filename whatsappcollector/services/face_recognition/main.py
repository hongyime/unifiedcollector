"""Face recognition service entrypoint."""
import asyncio
import bz2
import os
import signal
import sys
import urllib.request


_MODEL_SPECS = [
    (
        "shape_predictor_68_face_landmarks.dat",
        "http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2",
    ),
    (
        "dlib_face_recognition_resnet_model_v1.dat",
        "http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2",
    ),
]


def _ensure_models() -> None:
    """Download missing dlib model files before the worker starts."""
    models_dir = os.environ.get("FACE_MODELS_PATH", "/data/models")

    for filename, url in _MODEL_SPECS:
        dest_path = os.path.join(models_dir, filename)
        if os.path.exists(dest_path):
            print(f"[face_recognition] model already present: {dest_path}", flush=True)
            continue

        print(f"[face_recognition] downloading missing model: {filename} from {url}", flush=True)
        try:
            os.makedirs(models_dir, exist_ok=True)
            bz2_path = dest_path + ".bz2"

            def _reporthook(block_num: int, block_size: int, total_size: int) -> None:
                if total_size > 0 and block_num % 500 == 0:
                    downloaded = block_num * block_size
                    pct = min(100, downloaded * 100 // total_size)
                    print(f"[face_recognition] {filename}: {pct}% ({downloaded}/{total_size} bytes)", flush=True)

            urllib.request.urlretrieve(url, bz2_path, reporthook=_reporthook)

            with bz2.open(bz2_path, "rb") as src, open(dest_path, "wb") as dst:
                dst.write(src.read())

            os.remove(bz2_path)
            print(f"[face_recognition] model downloaded and extracted: {dest_path}", flush=True)

        except Exception as exc:
            print(
                f"[face_recognition] ERROR: failed to download {filename}: {exc}\n"
                f"  Manual download: {url}\n"
                f"  Expected destination: {dest_path}\n"
                f"  The worker will start in degraded mode.",
                file=sys.stderr,
                flush=True,
            )


async def _run_worker() -> None:
    from face_recognition_service.worker import worker
    from face_recognition_service.observability import get_logger

    logger = get_logger("face_recognition.main")
    logger.info("face_recognition_service_starting")

    await worker.start()

    stop_event = asyncio.Event()

    def _stop(*_args: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _stop)
    loop.add_signal_handler(signal.SIGTERM, _stop)

    await stop_event.wait()
    await worker.stop()


if __name__ == "__main__":
    _ensure_models()
    asyncio.run(_run_worker())
