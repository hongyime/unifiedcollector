"""Logging setup and configuration."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_file: Path, level: str = 'INFO') -> logging.Logger:
    """Set up logging with file and console handlers."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger('uttk')

    # Always apply the requested level (even if handlers already exist)
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)

    # Avoid duplicate handlers
    if logger.handlers:
        return logger
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s %(message)s'
    )
    
    # File handler with rotation
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=1_048_576,  # 1MB
        backupCount=3,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # Add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    logger.debug('Logging initialized')
    return logger
