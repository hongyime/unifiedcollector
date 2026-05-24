"""
Pytest configuration and shared fixtures for YouTube Toolkit tests.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent

# Add src/ so bare imports like `data_manager_streamlined`, `auth_cache` work
sys.path.insert(0, str(project_root / "src"))
# Add scripts/ so bare imports like `batch_downloader` work
sys.path.insert(0, str(project_root / "scripts"))
