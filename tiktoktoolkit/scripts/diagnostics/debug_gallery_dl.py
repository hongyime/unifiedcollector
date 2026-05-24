#!/usr/bin/env python3
"""
Standalone gallery-dl debug script for TikTok downloads.
This script helps diagnose why gallery-dl downloads are failing.
"""

import subprocess
import sys
import shutil
from pathlib import Path

def test_gallery_dl_installation():
    """Test if gallery-dl is installed and working."""
    print("🔍 Testing gallery-dl installation...")
    
    try:
        result = subprocess.run(['gallery-dl', '--version'], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print(f"✅ Gallery-dl found: {result.stdout.strip()}")
            return True
        else:
            print(f"❌ Gallery-dl version check failed: {result.stderr}")
            return False
    except FileNotFoundError:
        print("❌ Gallery-dl not found. Install with: pip3 install gallery-dl")
        return False
    except Exception as e:
        print(f"❌ Gallery-dl test error: {e}")
        return False

def test_tiktok_access(username="rhonduhhhh"):
    """Test TikTok access for a specific user."""
    print(f"\\n🧪 Testing TikTok access for user: @{username}")
    
    test_url = f"https://www.tiktok.com/@{username}"
    
    # Test 1: List URLs (doesn't download, just checks what's available)
    print("\\n📋 Step 1: Checking what URLs gallery-dl can find...")
    try:
        args = ['gallery-dl', '--list-urls', '--range', '1-2', test_url]
        print(f"Command: {' '.join(args)}")
        
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        
        print(f"Return code: {result.returncode}")
        
        if result.stdout:
            lines = result.stdout.strip().split('\\n')
            print(f"STDOUT ({len(lines)} lines):")
            for i, line in enumerate(lines[:10]):  # Show first 10 lines
                print(f"  {i+1}: {line}")
            if len(lines) > 10:
                print(f"  ... and {len(lines) - 10} more lines")
        
        if result.stderr:
            print(f"STDERR:\\n{result.stderr}")
        
        # Analyze results
        if result.returncode == 0:
            if result.stdout and 'tiktok.com' in result.stdout:
                video_urls = [line for line in result.stdout.split('\\n') if 'video' in line]
                print(f"✅ Found {len(video_urls)} video URLs")
                return True
            else:
                print("⚠️  Gallery-dl ran successfully but found no video URLs")
                print("   This usually means:")
                print("   - Account is private")
                print("   - Account has no videos") 
                print("   - Account doesn't exist")
                print("   - TikTok is blocking access")
                return False
        else:
            print("❌ Gallery-dl failed to list URLs")
            return False
            
    except subprocess.TimeoutExpired:
        print("❌ Command timed out (>60 seconds)")
        return False
    except Exception as e:
        print(f"❌ Error testing URL access: {e}")
        return False

def test_download_simulation(username="rhonduhhhh"):
    """Test a simulated download (dry run)."""
    print(f"\\n🎬 Step 2: Testing download simulation for @{username}...")
    
    test_url = f"https://www.tiktok.com/@{username}"
    test_dir = Path("test_downloads")
    test_dir.mkdir(exist_ok=True)
    
    try:
        # Use --simulate to test download without actually downloading
        args = [
            'gallery-dl', 
            '--simulate',  # Don't actually download
            '--verbose',   # More output
            '--range', '1-1',  # Just test first video
            '--dest', str(test_dir),
            test_url
        ]
        
        print(f"Command: {' '.join(args)}")
        
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        
        print(f"Return code: {result.returncode}")
        
        if result.stdout:
            print(f"STDOUT:\\n{result.stdout}")
        
        if result.stderr:
            print(f"STDERR:\\n{result.stderr}")
        
        # Clean up test directory
        try:
            shutil.rmtree(test_dir, ignore_errors=True)
        except:
            pass
        
        if result.returncode == 0:
            print("✅ Simulation successful - downloads should work")
            return True
        else:
            print("❌ Simulation failed")
            return False
            
    except Exception as e:
        print(f"❌ Simulation error: {e}")
        return False

def main():
    """Run all diagnostic tests."""
    print("🩺 TikTok Gallery-dl Diagnostic Tool")
    print("=" * 50)
    
    # Test 1: Installation
    if not test_gallery_dl_installation():
        print("\\n❌ Gallery-dl is not properly installed. Please fix this first.")
        return False
    
    # Test 2: TikTok access
    username = input("\\nEnter username to test (default: rhonduhhhh): ").strip() or "rhonduhhhh"
    username = username.lstrip('@')  # Remove @ if present
    
    urls_work = test_tiktok_access(username)
    
    if urls_work:
        # Test 3: Download simulation
        download_works = test_download_simulation(username)
        
        if download_works:
            print("\\n🎉 All tests passed! Downloads should work.")
            print("\\nIf you're still having issues:")
            print("1. The user might have no new videos (all already downloaded)")
            print("2. Try a different user")
            print("3. Check your internet connection")
        else:
            print("\\n⚠️  URL listing works but download simulation failed")
            print("This might indicate authentication or rate limiting issues")
    else:
        print("\\n❌ TikTok access failed")
        print("\\nPossible solutions:")
        print("1. Try a known public account like 'tiktok'")
        print("2. Check if the username is correct")
        print("3. The account might be private or banned")
        print("4. TikTok might be blocking gallery-dl")
    
    print("\\n" + "=" * 50)
    return True

if __name__ == "__main__":
    main()
