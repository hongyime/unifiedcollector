#!/usr/bin/env python3
"""Strava Toolkit - Main entry point."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from ingestion.cli import main
if __name__ == "__main__":
    main()
