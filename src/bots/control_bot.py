"""Dedicated entrypoint for the Telegram control-room bot."""
import asyncio
import logging

from src.notifications.collector_bot import run_control_bot_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] control-bot: %(message)s")

if __name__ == "__main__":
    asyncio.run(run_control_bot_service())
