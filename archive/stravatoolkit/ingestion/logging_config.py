"""Logging configuration for the ingestion system."""
from __future__ import annotations

import logging
import os
import sys


def configure_logging() -> None:
    """
    Configure structured logging for the ingestion system.
    
    Log level is controlled by the LOG_LEVEL environment variable.
    Default level is INFO.
    
    Valid levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    
    # Validate level
    numeric_level = getattr(logging, level, None)
    if not isinstance(numeric_level, int):
        print(f"[warning] Invalid LOG_LEVEL '{level}', defaulting to INFO", file=sys.stderr)
        numeric_level = logging.INFO
    
    # Configure root logger
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    
    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for the given module name.
    
    Args:
        name: Module name (typically __name__)
    
    Returns:
        Logger instance
    """
    return logging.getLogger(name)
