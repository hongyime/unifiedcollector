#!/usr/bin/env python3
"""Lemon8 Toolkit - Main entry point."""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import and run the CLI
from src.main import main

if __name__ == "__main__":
    main()
