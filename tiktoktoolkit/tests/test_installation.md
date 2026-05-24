# Global CLI Installation Testing

This document provides instructions for testing the global CLI installation of the TikTok Toolkit.

## Prerequisites

- Python 3.8 or higher
- pip package manager
- Virtual environment tool (venv or virtualenv)

## Test Procedure

### 1. Create Clean Test Environment

```bash
# Create a new virtual environment
python -m venv test_env

# Activate it
# Windows:
test_env\Scripts\activate
# Unix/Mac:
source test_env/bin/activate
```

### 2. Install the Package

```bash
# From the project root directory
pip install .

# Or for development mode (editable install):
pip install -e .
```

### 3. Verify Installation

```bash
# Check that uttk command is available
uttk --help

# Should display:
# Usage: uttk [OPTIONS] COMMAND [ARGS]...
#   Unified TikTok Toolkit - Download TikTok videos using Gallery-dl.
# ...
```

### 4. Test Basic Commands

```bash
# Test help for download command
uttk download --help

# Test help for utils command
uttk utils --help

# Test version (if implemented)
uttk --version
```

### 5. Test Actual Download (Optional)

```bash
# Test with a public TikTok account
uttk download user --user tiktok --limit 1 --out ./test_downloads

# Verify files were downloaded
ls ./test_downloads/username_tiktok/
```

### 6. Test Cookie Setup

```bash
# Test cookie extraction (requires browser)
uttk utils setup-cookies --browser chrome

# Verify cookies file was created
ls configs/tiktok_cookies.txt
```

### 7. Clean Up

```bash
# Deactivate virtual environment
deactivate

# Remove test environment
rm -rf test_env

# Remove test downloads
rm -rf test_downloads
```

## Expected Results

### Successful Installation

- ✅ `uttk` command is available in PATH
- ✅ `uttk --help` displays usage information
- ✅ All subcommands are accessible
- ✅ Downloads work correctly
- ✅ Configuration files are created in correct locations

### Common Issues

#### Issue: `uttk: command not found`

**Cause:** Package not installed or not in PATH

**Solution:**
```bash
# Verify installation
pip list | grep unified-tiktok-toolkit

# Reinstall if needed
pip install --force-reinstall .
```

#### Issue: `ModuleNotFoundError: No module named 'src'`

**Cause:** Package not installed, or running from wrong directory

**Solution:**
```bash
# Run from project root (where main.py lives)
# Or use editable install for development
pip install -e .
```

#### Issue: `gallery-dl: command not found`

**Cause:** gallery-dl dependency not installed

**Solution:**
```bash
# Install dependencies
pip install -r requirements.txt
```

## Automated Test Script

```python
#!/usr/bin/env python3
"""Automated installation test script."""

import subprocess
import sys
from pathlib import Path


def run_command(cmd, check=True):
    """Run command and return result."""
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"ERROR: Command failed with code {result.returncode}")
        print(f"STDERR: {result.stderr}")
        sys.exit(1)
    return result


def test_installation():
    """Test global CLI installation."""
    print("=" * 70)
    print("Testing Global CLI Installation")
    print("=" * 70)
    
    # Test 1: Check uttk command exists
    print("\n[1/5] Checking uttk command...")
    result = run_command(['uttk', '--help'])
    assert 'Unified TikTok Toolkit' in result.stdout
    print("✅ uttk command found")
    
    # Test 2: Check download command
    print("\n[2/5] Checking download command...")
    result = run_command(['uttk', 'download', '--help'])
    assert 'Download TikTok videos' in result.stdout
    print("✅ download command works")
    
    # Test 3: Check utils command
    print("\n[3/5] Checking utils command...")
    result = run_command(['uttk', 'utils', '--help'])
    assert 'Utility commands' in result.stdout
    print("✅ utils command works")
    
    # Test 4: Check dependencies
    print("\n[4/5] Checking dependencies...")
    result = run_command(['gallery-dl', '--version'])
    print(f"✅ gallery-dl version: {result.stdout.strip()}")
    
    # Test 5: Check package info
    print("\n[5/5] Checking package info...")
    result = run_command(['pip', 'show', 'unified-tiktok-toolkit'])
    assert 'Name: unified-tiktok-toolkit' in result.stdout
    print("✅ Package installed correctly")
    
    print("\n" + "=" * 70)
    print("All tests passed! ✅")
    print("=" * 70)


if __name__ == '__main__':
    try:
        test_installation()
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
```

## Documentation Updates

After testing, update the following sections in README.md:

1. **Installation section**: Confirm installation steps work
2. **Usage section**: Verify all command examples are correct
3. **Troubleshooting section**: Add any issues discovered during testing

## Notes

- Test on multiple platforms if possible (Windows, Mac, Linux)
- Test with both Python 3.8 and latest Python version
- Document any platform-specific issues
- Update setup.py if entry points need adjustment
