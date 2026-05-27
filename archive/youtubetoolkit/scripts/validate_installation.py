#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Installation Validation Script
Verifies that all components are properly installed and configured.
"""
import sys
import os
from pathlib import Path

# Set UTF-8 encoding for Windows console
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# Add src and scripts to path (this script lives in scripts/, so root is one level up)
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

def print_header(text):
    """Print a formatted header."""
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}\n")

def check_python_version():
    """Check Python version."""
    print("🐍 Checking Python version...")
    version = sys.version_info
    if version.major >= 3 and version.minor >= 10:
        print(f"   ✅ Python {version.major}.{version.minor}.{version.micro} (OK)")
        return True
    else:
        print(f"   ❌ Python {version.major}.{version.minor}.{version.micro} (Need 3.10+)")
        return False

def check_dependencies():
    """Check if required dependencies are installed."""
    print("\n📦 Checking dependencies...")
    required = [
        'yt_dlp',
        'google.auth',
        'googleapiclient',
        'tqdm'
    ]
    
    all_ok = True
    for module in required:
        try:
            __import__(module)
            print(f"   ✅ {module}")
        except ImportError:
            print(f"   ❌ {module} (missing)")
            all_ok = False
    
    return all_ok

def check_core_modules():
    """Check if core modules can be imported."""
    print("\n🔧 Checking core modules...")
    modules = [
        'app_paths',
        'config',
        'data_manager_streamlined',
        'auth_cache',
        'video_processor',
        'batch_downloader'
    ]
    
    all_ok = True
    for module in modules:
        try:
            __import__(module)
            print(f"   ✅ {module}")
        except Exception as e:
            print(f"   ❌ {module} ({str(e)})")
            all_ok = False
    
    return all_ok

def check_app_data():
    """Check data directory structure."""
    print("\n📁 Checking data directory...")
    
    from app_paths import DATA_DIR, ensure_app_data_dir
    
    ensure_app_data_dir()
    
    if DATA_DIR.exists():
        print(f"   ✅ data/ exists at {DATA_DIR}")
    else:
        print(f"   ❌ data/ not found")
        return False
    
    # Check for important files
    files = {
        'config.json': 'Configuration file',
        'youtube_data.db': 'Database (created on first run)',
        'client_secret.json': 'OAuth credentials (user must provide)'
    }
    
    for filename, description in files.items():
        filepath = DATA_DIR / filename
        if filepath.exists():
            print(f"   ✅ {filename} - {description}")
        else:
            if filename == 'client_secret.json':
                print(f"   ⚠️  {filename} - {description} (optional for OAuth)")
            else:
                print(f"   ℹ️  {filename} - {description} (will be created)")
    
    return True

def check_database():
    """Check database integrity."""
    print("\n💾 Checking database...")
    
    try:
        from data_manager_streamlined import DatabaseManager
        db = DatabaseManager()
        
        # Try to get statistics
        stats = db.get_video_statistics()
        total = sum(stats.get('videos_by_status', {}).values())
        
        print(f"   ✅ Database initialized")
        print(f"   ℹ️  Total videos in queue: {total}")
        
        if total > 0:
            status_counts = stats.get('videos_by_status', {})
            for status, count in status_counts.items():
                print(f"      - {status}: {count}")
        
        return True
    except Exception as e:
        print(f"   ❌ Database error: {e}")
        return False

def check_tests():
    """Check if tests can be discovered."""
    print("\n🧪 Checking test suite...")
    
    test_dir = ROOT / 'tests'
    if not test_dir.exists():
        print("   ❌ tests/ directory not found")
        return False
    
    test_files = list(test_dir.glob('test_*.py'))
    if test_files:
        print(f"   ✅ Found {len(test_files)} test files")
        for test_file in test_files:
            print(f"      - {test_file.name}")
        print("\n   ℹ️  Run tests with: pytest")
        return True
    else:
        print("   ⚠️  No test files found")
        return False

def check_documentation():
    """Check documentation files."""
    print("\n📚 Checking documentation...")
    
    docs = {
        'README.md': 'User documentation',
        'docs/PRD.md': 'Product requirements',
        'QUICK_START.txt': 'Quick start guide'
    }
    
    all_ok = True
    for filepath, description in docs.items():
        if (ROOT / filepath).exists():
            print(f"   ✅ {filepath} - {description}")
        else:
            print(f"   ⚠️  {filepath} - {description} (missing)")
            all_ok = False
    
    return all_ok

def main():
    """Run all validation checks."""
    print_header("YouTube Toolkit Installation Validation")
    
    checks = [
        ("Python Version", check_python_version),
        ("Dependencies", check_dependencies),
        ("Core Modules", check_core_modules),
        ("App Data", check_app_data),
        ("Database", check_database),
        ("Tests", check_tests),
        ("Documentation", check_documentation)
    ]
    
    results = []
    for name, check_func in checks:
        try:
            result = check_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n   ❌ Error during {name} check: {e}")
            results.append((name, False))
    
    # Summary
    print_header("Validation Summary")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"   {status} - {name}")
    
    print(f"\n   Score: {passed}/{total} checks passed")
    
    if passed == total:
        print("\n   🎉 All checks passed! Installation is valid.")
        print("\n   Next steps:")
        print("   1. Add client_secret.json to data/ for OAuth")
        print("   2. Run: start_toolkit.bat")
        print("   3. Or run: python batch_downloader.py")
        return 0
    else:
        print("\n   ⚠️  Some checks failed. Review the output above.")
        print("\n   Common fixes:")
        print("   - Run: pip install -r requirements.txt")
        print("   - Ensure Python 3.10+ is installed")
        print("   - Check that all files are present")
        return 1

if __name__ == '__main__':
    sys.exit(main())
