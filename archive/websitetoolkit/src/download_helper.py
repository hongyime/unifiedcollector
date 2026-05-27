#!/usr/bin/env python3
"""
Download Directory Helper - Always prompts user for download location
"""
import os
from typing import Optional

def prompt_for_download_location(context: str = "photos", default_fallback: str = "downloads") -> str:
    """
    Always prompt user for download location, with fallback to default only as last resort.
    
    Args:
        context: Description of what's being downloaded (e.g., "photos", "links")
        default_fallback: Default directory name to use as fallback
    
    Returns:
        str: Validated download directory path
    """
    print(f"\n📁 DOWNLOAD LOCATION FOR {context.upper()}")
    print("=" * 50)
    print("Please specify where to save downloaded content.")
    print("Examples:")
    print(f"  • Custom: C:\\Users\\YourName\\Pictures\\{context}")
    print(f"  • Custom: /home/user/Documents/{context}")
    print(f"  • Relative: ./{context}_downloads")
    print(f"  • Current dir: ./{default_fallback}")
    print()
    
    while True:
        download_path = input(f"📂 Enter download directory path (or 'default' for {default_fallback}): ").strip()
        
        if not download_path:
            print("❌ Please enter a valid path or 'default'")
            continue
            
        if download_path.lower() == 'default':
            download_path = default_fallback
            print(f"📁 Using default: {os.path.abspath(download_path)}")
        
        # Validate and create directory
        try:
            # Expand user path if needed (~)
            download_path = os.path.expanduser(download_path)
            
            # Create directory if it doesn't exist
            os.makedirs(download_path, exist_ok=True)
            
            # Test write access
            test_file = os.path.join(download_path, '.write_test')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            
            print(f"✅ Download location confirmed: {os.path.abspath(download_path)}")
            return os.path.abspath(download_path)
            
        except PermissionError:
            print(f"❌ Permission denied: {download_path}")
            print("   Please choose a different location or run with appropriate permissions.")
        except Exception as e:
            print(f"❌ Invalid path: {download_path}")
            print(f"   Error: {e}")
            print("   Please try a different path.")

def prompt_for_website_info() -> tuple[str, Optional[str]]:
    """
    Prompt user for website information when auto-adding to config.
    
    Returns:
        tuple: (website_name, website_url)
    """
    print(f"\n🌐 WEBSITE INFORMATION")
    print("=" * 30)
    print("Since you're using a custom download path, we'll add this website to your config.")
    
    while True:
        website_name = input("📝 Enter website name (e.g., 'Example Site'): ").strip()
        if website_name:
            break
        print("❌ Please enter a valid website name")
    
    website_url = input("🔗 Enter website URL (optional, press Enter to skip): ").strip()
    website_url = website_url if website_url else None
    
    return website_name, website_url