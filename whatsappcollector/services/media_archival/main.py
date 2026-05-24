"""Media archival service entrypoint."""
import asyncio
import signal


async def _run_worker() -> None:
    from media_archival.worker import worker
    from media_archival.observability import get_logger

    logger = get_logger("media_archival.main")
    logger.info("media_archival_service_starting")

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
    asyncio.run(_run_worker())
