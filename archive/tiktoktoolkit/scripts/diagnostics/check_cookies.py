#!/usr/bin/env python3
"""
Standalone TikTok Cookie Checker
This script helps verify if your TikTok cookies are working correctly with gallery-dl.
"""

import subprocess
import sys
from pathlib import Path
import json

def find_cookies_file():
    """Find TikTok cookies file in common locations."""
    possible_locations = [
        # Toolkit specific locations
        Path("configs/tiktok_cookies.txt"),
        Path("cookies.txt"),
        Path("tiktok_cookies.txt"),
        
        # Gallery-dl default locations
        Path.home() / ".config/gallery-dl/cookies.txt",
        Path.home() / ".local/share/gallery-dl/cookies.txt",
        
        # Windows locations
        Path.home() / "AppData/Roaming/gallery-dl/cookies.txt",
        
        # Current directory
        Path.cwd() / "cookies.txt",
    ]
    
    found_files = []
    for location in possible_locations:
        if location.exists() and location.stat().st_size > 0:
            found_files.append(location)
    
    return found_files

def check_cookies_format(cookies_file):
    """Check if cookies file has valid format."""
    try:
        with open(cookies_file, 'r') as f:
            content = f.read().strip()
        
        if not content:
            return False, "File is empty"
        
        lines = content.split('\\n')
        valid_lines = 0
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
                
            # Check if line looks like Netscape cookie format
            parts = line.split('\\t')
            if len(parts) >= 6:
                valid_lines += 1
            elif 'tiktok.com' in line.lower():
                valid_lines += 1
        
        if valid_lines > 0:
            return True, f"Found {valid_lines} valid cookie entries"
        else:
            return False, "No valid cookie entries found"
            
    except Exception as e:
        return False, f"Error reading file: {e}"

def test_gallery_dl_with_cookies(cookies_file, test_user="tiktok"):
    """Test gallery-dl with cookies."""
    print(f"🧪 Testing gallery-dl with cookies for @{test_user}...")
    
    test_url = f"https://www.tiktok.com/@{test_user}"
    
    # First try the newer --list-urls option
    cmd_new = [
        'gallery-dl', 
        '--cookies', str(cookies_file),
        '--list-urls', 
        '--range', '1-2',
        test_url
    ]
    
    print(f"Trying newer command: {' '.join(cmd_new)}")
    
    try:
        result = subprocess.run(cmd_new, capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            urls = [line.strip() for line in result.stdout.split('\\n') 
                   if line.strip() and 'tiktok.com' in line]
            
            print(f"✅ Success! Found {len(urls)} video URLs")
            
            if urls:
                print("Sample URLs:")
                for i, url in enumerate(urls[:3]):
                    print(f"  {i+1}. {url}")
                return True, len(urls)
            else:
                print("⚠️  No URLs found - account might be empty or private")
                return True, 0
        else:
            # Check if it's an "unrecognized arguments" error
            if "unrecognized arguments" in result.stderr and "--list-urls" in result.stderr:
                print("⚠️  --list-urls not supported, trying older method...")
                return test_gallery_dl_with_cookies_fallback(cookies_file, test_user)
            else:
                print("❌ Gallery-dl failed")
                if result.stderr:
                    print(f"Error: {result.stderr}")
                if result.stdout:
                    print(f"Output: {result.stdout}")
                return False, str(result.stderr)
            
    except subprocess.TimeoutExpired:
        print("❌ Command timed out")
        return False, "Timeout"
    except FileNotFoundError:
        print("❌ gallery-dl not found")
        return False, "gallery-dl not installed"
    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False, str(e)

def test_gallery_dl_with_cookies_fallback(cookies_file, test_user="tiktok"):
    """Test gallery-dl with cookies using older method (simulate download)."""
    print(f"🔄 Using fallback method for older gallery-dl version...")
    
    test_url = f"https://www.tiktok.com/@{test_user}"
    
    # Use --simulate to test without actually downloading
    cmd_fallback = [
        'gallery-dl', 
        '--cookies', str(cookies_file),
        '--simulate',  # Don't actually download
        '--range', '1-2',
        test_url
    ]
    
    print(f"Fallback command: {' '.join(cmd_fallback)}")
    
    try:
        result = subprocess.run(cmd_fallback, capture_output=True, text=True, timeout=60)
        
        print(f"Return code: {result.returncode}")
        
        if result.returncode == 0:
            # Count simulated downloads
            output_lines = result.stdout.split('\\n')
            simulated_count = 0
            
            for line in output_lines:
                line_lower = line.lower()
                # Look for simulation indicators
                if any(keyword in line_lower for keyword in [
                    'simulating', 'would download', 'skipping', 
                    'downloading', 'saving', '[tiktok]'
                ]):
                    simulated_count += 1
                    print(f"  📝 {line.strip()}")
            
            if simulated_count > 0:
                print(f"✅ Success! Gallery-dl can access {simulated_count} videos")
                return True, simulated_count
            else:
                # Check if there's any TikTok-related output
                tiktok_mentions = sum(1 for line in output_lines if 'tiktok' in line.lower())
                if tiktok_mentions > 0:
                    print(f"✅ Gallery-dl connected to TikTok (found {tiktok_mentions} references)")
                    print("⚠️  But no videos detected - account might be empty or private")
                    return True, 0
                else:
                    print("⚠️  No TikTok activity detected in output")
                    return False, "No TikTok activity"
        else:
            print("❌ Gallery-dl simulation failed")
            if result.stderr:
                print(f"Error: {result.stderr}")
            if result.stdout:
                print(f"Output: {result.stdout}")
            return False, str(result.stderr)
            
    except subprocess.TimeoutExpired:
        print("❌ Simulation timed out")
        return False, "Timeout"
    except Exception as e:
        print(f"❌ Simulation failed: {e}")
        return False, str(e)

def test_without_cookies(test_user="tiktok"):
    """Test gallery-dl without cookies for comparison."""
    print(f"\\n🔓 Testing WITHOUT cookies for @{test_user}...")
    
    test_url = f"https://www.tiktok.com/@{test_user}"
    
    # Try newer method first
    cmd_new = ['gallery-dl', '--list-urls', '--range', '1-2', test_url]
    
    try:
        result = subprocess.run(cmd_new, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            urls = [line.strip() for line in result.stdout.split('\\n') 
                   if line.strip() and 'tiktok.com' in line]
            print(f"📊 Without cookies: Found {len(urls)} URLs")
            return len(urls)
        elif "unrecognized arguments" in result.stderr and "--list-urls" in result.stderr:
            # Fall back to older method
            print("🔄 Using fallback method for comparison...")
            cmd_fallback = ['gallery-dl', '--simulate', '--range', '1-2', test_url]
            
            result = subprocess.run(cmd_fallback, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                # Count simulated downloads
                simulated_count = sum(1 for line in result.stdout.split('\\n') 
                                    if any(keyword in line.lower() for keyword in [
                                        'simulating', 'would download', 'skipping', 
                                        'downloading', 'saving', '[tiktok]'
                                    ]))
                print(f"📊 Without cookies: {simulated_count} videos detected")
                return simulated_count
            else:
                print("📊 Without cookies: Failed")
                return 0
        else:
            print("📊 Without cookies: Failed")
            return 0
            
    except Exception as e:
        print(f"📊 Without cookies test failed: {e}")
        return 0

def main():
    """Main cookie checking function."""
    print("🍪 TikTok Cookie Verification Tool")
    print("=" * 50)
    
    # Step 1: Find cookies files
    print("🔍 Searching for cookies files...")
    cookies_files = find_cookies_file()
    
    if not cookies_files:
        print("❌ No cookies files found!")
        print("\\n💡 Common locations checked:")
        print("  - configs/tiktok_cookies.txt")
        print("  - cookies.txt") 
        print("  - ~/.config/gallery-dl/cookies.txt")
        print("\\n🔧 To create cookies:")
        print("  1. Install browser-cookie3: pip install browser-cookie3")
        print("  2. Log into TikTok in your browser")
        print("  3. Run: python main.py utils setup-cookies --browser chrome")
        return False
    
    print(f"✅ Found {len(cookies_files)} cookies file(s):")
    for i, cf in enumerate(cookies_files, 1):
        size = cf.stat().st_size
        print(f"  {i}. {cf} ({size} bytes)")
    
    # Use the first/largest cookies file
    cookies_file = max(cookies_files, key=lambda f: f.stat().st_size)
    print(f"\\n📁 Using: {cookies_file}")
    
    # Step 2: Check cookies format
    print("\\n📋 Checking cookies format...")
    format_ok, format_msg = check_cookies_format(cookies_file)
    
    if format_ok:
        print(f"✅ {format_msg}")
    else:
        print(f"❌ {format_msg}")
        print("💡 Cookies might be corrupted. Try refreshing them.")
    
    # Step 3: Test with gallery-dl
    print("\\n" + "="*50)
    
    # Get test username
    test_user = input("Enter username to test (default: tiktok): ").strip() or "tiktok"
    test_user = test_user.lstrip('@')
    
    # Test with cookies
    print("\\n🧪 Testing with cookies...")
    success_with, urls_with = test_gallery_dl_with_cookies(cookies_file, test_user)
    
    # Test without cookies
    urls_without = test_without_cookies(test_user)
    
    # Step 4: Analysis
    print("\\n" + "="*50)
    print("📊 ANALYSIS:")
    
    if success_with:
        print("✅ Gallery-dl works with your cookies")
        
        if urls_with > urls_without:
            print(f"🎉 Cookies provide access to MORE content! ({urls_with} vs {urls_without} URLs)")
            print("✅ Your cookies are working perfectly!")
        elif urls_with == urls_without:
            if urls_with > 0:
                print(f"📊 Same access with/without cookies ({urls_with} URLs)")
                print("ℹ️  This is normal for public accounts")
            else:
                print("⚠️  No URLs found with or without cookies")
                print("💡 Account might be private, empty, or not exist")
        else:
            print("⚠️  Fewer URLs with cookies - this is unusual")
            print("🔄 Consider refreshing your cookies")
    else:
        print("❌ Gallery-dl failed with cookies")
        print("🔄 Recommendations:")
        print("  1. Refresh cookies (log out and back into TikTok)")
        print("  2. Check if you're logged into the correct TikTok account")
        print("  3. Try a different browser")
        print("  4. Verify gallery-dl is updated: pip install --upgrade gallery-dl")
    
    # Step 5: Recommendations
    print("\\n💡 RECOMMENDATIONS:")
    
    if success_with and urls_with > 0:
        print("✅ Your cookies are working correctly!")
        print("🎯 You can download from public accounts and any private accounts you follow")
    elif success_with and urls_with == 0:
        print("⚠️  Cookies work but no content found")
        print("🔍 Try testing with: @tiktok, @charlidamelio, or another public account")
    else:
        print("❌ Cookies need to be fixed")
        print("🔧 Steps to fix:")
        print("   1. Open TikTok in your browser")
        print("   2. Log out completely")
        print("   3. Log back in")
        print("   4. Re-extract cookies")
    
    return success_with

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\\n\\n⏹️  Cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\\n\\n❌ Unexpected error: {e}")
        sys.exit(1)
