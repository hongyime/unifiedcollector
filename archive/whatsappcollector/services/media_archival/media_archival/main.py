import asyncio
import signal

from .observability import get_logger
from .worker import worker

logger = get_logger(__name__)


async def run() -> None:
    await worker.start()
    stop_event = asyncio.Event()

    def _stop(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _stop)
    loop.add_signal_handler(signal.SIGTERM, _stop)
    await stop_event.wait()
    await worker.stop()


def main() -> None:
    logger.info("media_archival_service_starting")
    asyncio.run(run())


if __name__ == "__main__":
    main()
