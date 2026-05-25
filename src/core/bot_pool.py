import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class BotStatus(Enum):
    HEALTHY = "healthy"
    LOCKED = "locked"
    ERROR = "error"
    DISCONNECTED = "disconnected"


@dataclass
class Bot:
    name: str
    token: str
    client: object = None
    status: BotStatus = BotStatus.DISCONNECTED
    locked_until: float = 0.0
    error_count: int = 0
    last_used: float = 0.0
    last_health_check: float = 0.0


class BotPool:

    def __init__(self, default_lockout: float = 300.0, max_errors: int = 5,
                 health_interval: float = 30.0):
        self._bots: list[Bot] = []
        self._current_index = 0
        self._default_lockout = default_lockout
        self._max_errors = max_errors
        self._health_interval = health_interval
        self._health_task: asyncio.Task | None = None
        self._clock_offset: float = 0.0

    def add_bot(self, name: str, token: str):
        self._bots.append(Bot(name=name, token=token))

    def load_from_env(self, tokens_csv: str):
        for i, token in enumerate(t.strip() for t in tokens_csv.split(",") if t.strip()):
            self.add_bot(f"bot_{i}", token)

    def get_healthy_bot(self, exclude: str | None = None) -> Bot | None:
        now = time.monotonic()
        candidates = []
        for bot in self._bots:
            if bot.name == exclude:
                continue
            if bot.status == BotStatus.LOCKED and now >= bot.locked_until:
                bot.status = BotStatus.HEALTHY
                bot.error_count = 0
                logger.info("Bot %s lockout expired, marking healthy", bot.name)
            if bot.status in (BotStatus.HEALTHY, BotStatus.DISCONNECTED):
                candidates.append(bot)

        if not candidates:
            return None

        candidates.sort(key=lambda b: b.last_used)
        chosen = candidates[0]
        chosen.last_used = now
        return chosen

    def record_success(self, bot_name: str):
        bot = self._find(bot_name)
        if bot:
            bot.error_count = max(0, bot.error_count - 1)
            if bot.status == BotStatus.ERROR and bot.error_count == 0:
                bot.status = BotStatus.HEALTHY

    def record_error(self, bot_name: str):
        bot = self._find(bot_name)
        if not bot:
            return
        bot.error_count += 1
        if bot.error_count >= self._max_errors:
            bot.status = BotStatus.ERROR
            logger.warning("Bot %s hit max errors (%d), marked ERROR", bot_name, self._max_errors)

    def record_lockout(self, bot_name: str, seconds: float | None = None):
        bot = self._find(bot_name)
        if not bot:
            return
        duration = seconds or self._default_lockout
        bot.status = BotStatus.LOCKED
        bot.locked_until = time.monotonic() + duration
        logger.warning("Bot %s locked out for %.0fs", bot_name, duration)

    def get_recommendation(self, exclude: str | None = None) -> str | None:
        bot = self.get_healthy_bot(exclude=exclude)
        return bot.name if bot else None

    async def start_health_monitor(self, connect_fn=None):
        if self._health_task and not self._health_task.done():
            return
        self._health_task = asyncio.create_task(
            self._health_loop(connect_fn)
        )

    async def stop_health_monitor(self):
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

    async def _health_loop(self, connect_fn):
        while True:
            try:
                await asyncio.sleep(self._health_interval)
                now = time.monotonic()

                self._detect_clock_drift()

                for bot in self._bots:
                    if bot.status == BotStatus.LOCKED and now >= bot.locked_until:
                        bot.status = BotStatus.HEALTHY
                        bot.error_count = 0
                        logger.info("Bot %s lockout expired", bot.name)

                    if bot.client and hasattr(bot.client, 'is_connected'):
                        if not bot.client.is_connected():
                            bot.status = BotStatus.DISCONNECTED
                            if connect_fn:
                                try:
                                    await connect_fn(bot)
                                    bot.status = BotStatus.HEALTHY
                                    logger.info("Bot %s reconnected", bot.name)
                                except Exception as e:
                                    logger.debug("Bot %s reconnect failed: %s", bot.name, e)

                    bot.last_health_check = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Health check error: %s", e)

    def _detect_clock_drift(self):
        try:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                start = time.monotonic()
                sock.connect(("8.8.8.8", 53))
                rtt = time.monotonic() - start
            if rtt > 3.0:
                logger.warning("High RTT detected (%.1fs) — could indicate WSL2/Docker clock drift", rtt)
        except Exception:
            pass

    def _find(self, name: str) -> Bot | None:
        for bot in self._bots:
            if bot.name == name:
                return bot
        return None

    def get_status(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "name": b.name,
                "status": b.status.value,
                "error_count": b.error_count,
                "locked_remaining": max(0, b.locked_until - now) if b.status == BotStatus.LOCKED else 0,
            }
            for b in self._bots
        ]

    @property
    def size(self) -> int:
        return len(self._bots)

    @property
    def healthy_count(self) -> int:
        now = time.monotonic()
        return sum(
            1 for b in self._bots
            if b.status == BotStatus.HEALTHY or
            (b.status == BotStatus.LOCKED and now >= b.locked_until)
        )
