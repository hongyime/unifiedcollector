#!/usr/bin/env python3
"""
Progress Logger for Telegram Toolkit
Provides verbose progress logging with timestamps for all operations
"""
import sys
import time
from datetime import datetime
from typing import Optional
from src.core.console import configure_console_output


configure_console_output()


class ProgressLogger:
    """Logger with emoji and timestamps for progress tracking"""
    
    def __init__(self, name: str = "Operation"):
        self.name = name
        self.start_time: Optional[float] = None
        self.last_step_time: Optional[float] = None
        self.steps_completed = 0
        
    def _timestamp(self) -> str:
        """Get current timestamp string"""
        return datetime.now().strftime("%H:%M:%S")
    
    def _elapsed(self) -> str:
        """Get elapsed time since start"""
        if self.start_time is None:
            return ""
        elapsed = time.time() - self.start_time
        if elapsed < 60:
            return f"({elapsed:.1f}s)"
        elif elapsed < 3600:
            return f"({elapsed/60:.1f}m)"
        else:
            return f"({elapsed/3600:.1f}h)"
    
    def start(self, message: str) -> None:
        """Log operation start with [HH:MM:SS] 🚀 Start: message"""
        self.start_time = time.time()
        self.last_step_time = time.time()
        self.steps_completed = 0
        print(f"[{self._timestamp()}] 🚀 START: {message} {self._elapsed()}", flush=True)
        sys.stdout.flush()
    
    def step(self, message: str) -> None:
        """Log progress step with [HH:MM:SS] 🔄 Step: message"""
        self.steps_completed += 1
        elapsed_since_last = ""
        if self.last_step_time:
            delta = time.time() - self.last_step_time
            elapsed_since_last = f" (+{delta:.1f}s)"
        self.last_step_time = time.time()
        print(f"[{self._timestamp()}] 🔄 STEP {self.steps_completed}: {message} {elapsed_since_last}", flush=True)
        sys.stdout.flush()
    
    def info(self, message: str) -> None:
        """Log info message with [HH:MM:SS] ℹ️ Info: message"""
        print(f"[{self._timestamp()}] ℹ️  INFO: {message}", flush=True)
        sys.stdout.flush()
    
    def success(self, message: str) -> None:
        """Log success with [HH:MM:SS] ✅ Done: message"""
        print(f"[{self._timestamp()}] ✅ SUCCESS: {message} {self._elapsed()}", flush=True)
        sys.stdout.flush()
    
    def complete(self, message: str) -> None:
        """Log completion with [HH:MM:SS] 🎉 Complete: message"""
        print(f"[{self._timestamp()}] 🎉 COMPLETE: {message} {self._elapsed()}", flush=True)
        sys.stdout.flush()
    
    def error(self, message: str) -> None:
        """Log error with [HH:MM:SS] ❌ Error: message"""
        print(f"[{self._timestamp()}] ❌ ERROR: {message}", flush=True)
        sys.stdout.flush()
    
    def warning(self, message: str) -> None:
        """Log warning with [HH:MM:SS] ⚠️ Warning: message"""
        print(f"[{self._timestamp()}] ⚠️  WARNING: {message}", flush=True)
        sys.stdout.flush()
    
    def progress(self, current: int, total: int, message: str = "") -> None:
        """Log progress with percentage [HH:MM:SS] 📊 50% (100/200): message"""
        if total > 0:
            pct = (current / total) * 100
            print(f"[{self._timestamp()}] 📊 {pct:.1f}% ({current}/{total}): {message} {self._elapsed()}", flush=True)
        else:
            print(f"[{self._timestamp()}] 📊 ({current}): {message}", flush=True)
        sys.stdout.flush()


# Global logger instance
_logger: Optional[ProgressLogger] = None

def get_logger(name: str = "Operation") -> ProgressLogger:
    """Get or create a progress logger"""
    global _logger
    _logger = ProgressLogger(name)
    return _logger


def log_start(message: str) -> None:
    """Quick log start"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 START: {message}", flush=True)


def log_step(message: str) -> None:
    """Quick log step"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 STEP: {message}", flush=True)


def log_info(message: str) -> None:
    """Quick log info"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ℹ️  INFO: {message}", flush=True)


def log_success(message: str) -> None:
    """Quick log success"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ SUCCESS: {message}", flush=True)


def log_complete(message: str) -> None:
    """Quick log complete"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🎉 COMPLETE: {message}", flush=True)


def log_error(message: str) -> None:
    """Quick log error"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ ERROR: {message}", flush=True)


def log_warning(message: str) -> None:
    """Quick log warning"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️  WARNING: {message}", flush=True)


def log_progress(current: int, total: int, message: str = "") -> None:
    """Quick log progress percentage"""
    if total > 0:
        pct = (current / total) * 100
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📊 {pct:.1f}% ({current}/{total}): {message}", flush=True)
