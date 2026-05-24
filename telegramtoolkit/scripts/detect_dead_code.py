#!/usr/bin/env python3
"""
Dead Code Detection Script
Uses Vulture to identify unused code in the project

Usage:
    python scripts/detect_dead_code.py

Excludes:
- Test files (dead code in tests is intentional)
- Third-party libraries
- CLI entry points that are not imported internally
"""

import subprocess
import sys
from pathlib import Path

# Directory to scan
SCAN_DIR = Path("toolkit")

# Files and directories to exclude (Vulture options)
EXCLUDES = [
    "tests/",           # Test files
    "test_",            # Test file patterns
    "scripts/",         # Scripts directory
    "__pycache__/",     # Python cache
    ".mypy_cache/",     # Mypy cache
    "*.egg-info/",      # Package metadata
]

# Functions/classes that are used but not detected by Vulture
# (CLI entry points, dynamically called code, etc.)
FALSE_POSITIVES = {
    # CLI entry points in main.py
    "ToolkitCLI.main",
    
    # Main.py orchestrator methods
    "ToolkitCLI.run_analyze_users",
    "ToolkitCLI.run_collect_links",
    "ToolkitCLI.run_backup",
    "ToolkitCLI.run_resender",
    "ToolkitCLI.send_photos",
    "ToolkitCLI.manage_accounts",
    "ToolkitCLI.manage_download_state",
    
    # Server entry points
    "APIHandler",
    "main",  # In api_server.py __main__
    
    # Manager classes (CLI tools)
    "GroupJoiner",
    "GroupCleaner",
    "ProfilePhotoDownloader",
    "AccountManager",
    "PhotoSender",
    "DownloadStateManager",
    
    # Standalone runners
    "MultiPlatformLinkCollector",
    
    # Base classes and abstract methods
    "BaseFeature",
    "FeatureProcessor",
}

def run_vulture():
    """Run vulture dead code analysis"""
    
    # Build vulture command
    cmd = [
        sys.executable, "-m", "vulture",
        str(SCAN_DIR),
        "main.py",
        "--min-confidence", "70",  # Only report high-confidence dead code
        "--sort-by-size",
    ]
    
    # Add exclusions
    for excl in EXCLUDES:
        cmd.extend(["--exclude", excl])
    
    print("=" * 80)
    print("🦉 VULTURE DEAD CODE DETECTION")
    print("=" * 80)
    print(f"\n📁 Scanning directory: {SCAN_DIR}")
    print(f"⚙️  Minimum confidence: 70%")
    print(f"🚫 Excluding: {', '.join(EXCLUDES[:3])}...")
    print("\n" + "=" * 80 + "\n")
    
    # Run vulture
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    # Print output
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr, file=sys.stderr)
    
    # Parse results
    lines = result.stdout.split('\n')
    unused_code_count = sum(1 for line in lines if line.strip() and not line.startswith('vulture:') and not line.startswith('    '))
    
    print("\n" + "=" * 80)
    
    if result.returncode == 0 and unused_code_count == 0:
        print("✅ NO DEAD CODE DETECTED")
        print("   Your codebase is clean!")
    else:
        print(f"⚠️  FOUND {unused_code_count} ITEMS OF POTENTIALLY DEAD CODE")
        print("\nNOTE: Some items may be false positives, such as:")
        print("  - CLI entry points (not imported by other modules)")
        print("  - Dynamically invoked code")
        print("  - Test utilities")
        print("  - Abstract base classes")
        print("\nReview the report above and determine if items are truly unused.")
    
    print("=" * 80)
    
    return result.returncode

if __name__ == "__main__":
    exit_code = run_vulture()
    sys.exit(exit_code)
