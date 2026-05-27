"""
Base command class for Instagram Toolkit.

Provides a common interface for all CLI commands with standardized
argument handling, validation, and error reporting.
"""
from __future__ import annotations

import argparse
from typing import List, Optional, Dict, Any
from abc import ABC, abstractmethod


class BaseCommand(ABC):
    """Base class for all CLI commands."""
    
    name: str = ""
    description: str = ""
    help_text: str = ""
    
    def __init__(self, parser: argparse.ArgumentParser):
        """
        Initialize command with argument parser.
        
        Args:
            parser: Subparser to add arguments to
        """
        self.parser = parser
        self._add_arguments()
    
    @abstractmethod
    def _add_arguments(self):
        """Add command-specific arguments to parser."""
        pass
    
    @abstractmethod
    def execute(self, args: argparse.Namespace) -> int:
        """
        Execute the command.
        
        Args:
            args: Parsed command arguments
            
        Returns:
            Exit code (0 for success, non-zero for failure)
        """
        pass
    
    def validate_args(self, args: argparse.Namespace) -> List[str]:
        """
        Validate command arguments.
        
        Args:
            args: Parsed command arguments
            
        Returns:
            List of validation error messages (empty if valid)
        """
        return []
    
    def print_error(self, message: str):
        """Print formatted error message."""
        print(f"[ERROR] {message}")
    
    def print_warning(self, message: str):
        """Print formatted warning message."""
        print(f"[WARNING] {message}")
    
    def print_info(self, message: str):
        """Print formatted info message."""
        print(f"[INFO] {message}")
    
    def print_success(self, message: str):
        """Print formatted success message."""
        print(f"[OK] {message}")


__all__ = ["BaseCommand"]


