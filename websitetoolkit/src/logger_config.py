import logging
import os
import sys
from logging.handlers import RotatingFileHandler

def setup_logger(name: str) -> logging.Logger:
    """Setup and return a logger with console and file handlers."""
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        
        # Ensure log directory exists — anchored to project root (one level above src/)
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # File handler (rotating)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, 'toolkit.log'),
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
    return logger
