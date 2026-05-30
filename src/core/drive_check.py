import logging
import os
import time

logger = logging.getLogger(__name__)

DRIVE_PATH = os.environ.get("COLLECTOR_DRIVE_PATH", "Z:/unifiedcollector/media")


def check_drive(path: str = DRIVE_PATH) -> bool:
    """Return True if the external drive is mounted and writable."""
    if not os.path.isdir(path):
        logger.warning("Drive not found: %s", path)
        return False
    # Per-process, per-call unique probe filename so concurrent collectors
    # don't race on the same `.drive_check` path.
    test_file = os.path.join(path, f".drive_check.{os.getpid()}.{time.time_ns()}")
    try:
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return True
    except OSError as e:
        logger.warning("Drive not writable: %s — %s", path, e)
        try:
            os.remove(test_file)
        except OSError:
            pass
        return False


def wait_for_drive(
    path: str = DRIVE_PATH,
    poll_interval: float = 30.0,
    stop_event=None,
) -> bool:
    """Block until the drive appears or stop_event is set.

    Returns True when drive is available, False if stopped.
    """
    while True:
        if check_drive(path):
            logger.info("Drive available: %s", path)
            return True
        if stop_event and stop_event.is_set():
            return False
        logger.info("Waiting for drive %s — retrying in %.0fs", path, poll_interval)
        if stop_event:
            stop_event.wait(poll_interval)
        else:
            time.sleep(poll_interval)
