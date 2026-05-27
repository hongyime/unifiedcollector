"""
Unified Website Toolkit - Bulk Website Importer
Safely import websites from text files without disrupting existing configuration
"""
import os
import json
import re
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
from urllib.parse import urlparse

from config import get_config, save_config
from utils import validate_website_url, get_domain_name


class BulkWebsiteImporter:
    """Safely import websites from various text formats"""
    
    def __init__(self):
        self.config = get_config()
        self.imported_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self.results = []
    
    def import_from_file(self, file_path: str, auto_enable: bool = True, 
                        skip_duplicates: bool = True) -> Dict[str, Any]:
        """Import websites from a text file"""
        if not os.path.exists(file_path):
            return {"error": f"File not found: {file_path}"}
        
        print(f"🔄 Importing websites from: {file_path}")
        print("=" * 60)
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Parse websites from file content
            websites = self._parse_websites_from_text(content)
            
            if not websites:
                return {"error": "No valid websites found in file"}
            
            print(f"📋 Found {len(websites)} potential websites to import")
            print()
            
            # Process each website
            for website_data in websites:
                self._process_website(website_data, auto_enable, skip_duplicates)
            
            # Save configuration if any websites were imported
            if self.imported_count > 0:
                success = self.config.save_config()
                if not success:
                    return {"error": "Failed to save configuration"}
            
            return self._generate_summary()
            
        except Exception as e:
            return {"error": f"Import failed: {e}"}
    
    def _parse_websites_from_text(self, content: str) -> List[Dict[str, str]]:
        """Parse websites from various text formats"""
        websites = []
        lines = content.strip().split('\n')
        
        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            
            # Skip empty lines and comments
            if not line or line.startswith('#') or line.startswith('//'):
                continue
            
            website_data = self._parse_single_line(line, line_num)
            if website_data:
                websites.append(website_data)
        
        return websites
    
    def _parse_single_line(self, line: str, line_num: int) -> Optional[Dict[str, str]]:
        """Parse a single line to extract website information"""
        # Remove common prefixes/suffixes
        line = line.strip().rstrip(',;')
        
        # Format 1: Just URL
        if line.startswith(('http://', 'https://')):
            return {
                'url': line,
                'name': self._generate_name_from_url(line),
                'source_line': line_num,
                'original_text': line
            }
        
        # Format 2: Name | URL or Name - URL
        if '|' in line:
            parts = line.split('|', 1)
            name = parts[0].strip()
            url = parts[1].strip()
            if self._is_valid_url(url):
                return {
                    'name': name,
                    'url': url,
                    'source_line': line_num,
                    'original_text': line
                }
        
        if ' - ' in line:
            parts = line.split(' - ', 1)
            name = parts[0].strip()
            url = parts[1].strip()
            if self._is_valid_url(url):
                return {
                    'name': name,
                    'url': url,
                    'source_line': line_num,
                    'original_text': line
                }
        
        # Format 3: Name: URL
        if ':' in line and not line.startswith(('http://', 'https://')):
            parts = line.split(':', 1)
            name = parts[0].strip()
            url = parts[1].strip()
            if self._is_valid_url(url):
                return {
                    'name': name,
                    'url': url,
                    'source_line': line_num,
                    'original_text': line
                }
        
        # Format 4: JSON-like format
        if line.startswith('{') and line.endswith('}'):
            try:
                data = json.loads(line)
                if 'url' in data:
                    return {
                        'name': data.get('name', self._generate_name_from_url(data['url'])),
                        'url': data['url'],
                        'source_line': line_num,
                        'original_text': line,
                        'notes': data.get('notes', ''),
                        'max_depth': data.get('max_depth', 3)
                    }
            except json.JSONDecodeError:
                pass
        
        # Format 5: Try to extract URL from anywhere in the line
        url_match = re.search(r'https?://[^\s<>"]+', line)
        if url_match:
            url = url_match.group()
            # Use the text before the URL as name, or generate from URL
            name_part = line[:url_match.start()].strip()
            name = name_part if name_part else self._generate_name_from_url(url)
            return {
                'name': name,
                'url': url,
                'source_line': line_num,
                'original_text': line
            }
        
        return None
    
    def _is_valid_url(self, url: str) -> bool:
        """Check if URL is valid"""
        try:
            if not url.startswith(('http://', 'https://')):
                return False
            
            parsed = urlparse(url)
            return bool(parsed.netloc)
        except:
            return False
    
    def _generate_name_from_url(self, url: str) -> str:
        """Generate a readable name from URL"""
        try:
            domain = get_domain_name(url)
            if domain.startswith('www.'):
                domain = domain[4:]
            
            # Convert domain to readable name
            name = domain.replace('.', '_').replace('-', '_')
            return name
        except:
            return url
    
    def _process_website(self, website_data: Dict[str, str], auto_enable: bool, 
                        skip_duplicates: bool):
        """Process a single website for import"""
        name = website_data['name']
        url = website_data['url']
        line_num = website_data['source_line']
        
        print(f"Line {line_num}: Processing {name} -> {url}")
        
        # Validate URL
        is_valid, validation_message = validate_website_url(url)
        if not is_valid:
            print(f"  ❌ {validation_message}")
            self.error_count += 1
            self.results.append({
                'line': line_num,
                'name': name,
                'url': url,
                'status': 'error',
                'reason': validation_message
            })
            return
        
        # Check for duplicates
        if skip_duplicates:
            is_duplicate, reason = self.config._is_duplicate_website(name, url)
            if is_duplicate:
                print(f"  ⏭️ Skipped: {reason}")
                self.skipped_count += 1
                self.results.append({
                    'line': line_num,
                    'name': name,
                    'url': url,
                    'status': 'skipped',
                    'reason': reason
                })
                return
        
        # Add website
        try:
            success = self.config.add_website(
                name_or_url=name,
                url=url,
                max_depth=website_data.get('max_depth', 3),
                notes=website_data.get('notes', f'Imported from bulk import on {datetime.now().strftime("%Y-%m-%d")}'),
                created_at=datetime.now().isoformat()
            )
            
            if success:
                print(f"  ✅ Added successfully")
                self.imported_count += 1
                self.results.append({
                    'line': line_num,
                    'name': name,
                    'url': url,
                    'status': 'imported',
                    'reason': 'Successfully added'
                })
                
                # Enable/disable as requested
                if not auto_enable:
                    self.config.toggle_website(name)
                    print(f"  🔧 Disabled (as requested)")
            else:
                print(f"  ❌ Failed to add")
                self.error_count += 1
                self.results.append({
                    'line': line_num,
                    'name': name,
                    'url': url,
                    'status': 'error',
                    'reason': 'Failed to add to config'
                })
                
        except Exception as e:
            print(f"  ❌ Error: {e}")
            self.error_count += 1
            self.results.append({
                'line': line_num,
                'name': name,
                'url': url,
                'status': 'error',
                'reason': str(e)
            })
    
    def _generate_summary(self) -> Dict[str, Any]:
        """Generate import summary"""
        return {
            'imported': self.imported_count,
            'skipped': self.skipped_count,
            'errors': self.error_count,
            'total_processed': len(self.results),
            'results': self.results
        }
    
    def create_sample_import_file(self, file_path: str = "sample_websites_import.txt"):
        """Create a sample import file showing supported formats"""
        sample_content = """# Bulk Website Import Sample File
# Lines starting with # are comments and will be ignored
# 
# Supported formats:
# 1. Just the URL:
https://example.com
https://github.com

# 2. Name | URL format:
Example Site | https://example.com
GitHub | https://github.com

# 3. Name - URL format:
Example Site - https://example.com
Stack Overflow - https://stackoverflow.com

# 4. Name: URL format:
Reddit: https://reddit.com
YouTube: https://youtube.com

# 5. JSON format (advanced):
{"name": "Custom Site", "url": "https://custom.com", "max_depth": 5, "notes": "Custom notes"}

# 6. Mixed text with URLs (will extract URLs):
Check out this cool site https://coolsite.com for inspiration
Visit https://news.ycombinator.com for tech news

# Examples with common sites:
Facebook | https://facebook.com
Twitter | https://twitter.com
Instagram | https://instagram.com
LinkedIn | https://linkedin.com
"""
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(sample_content)
            return file_path
        except Exception as e:
            print(f"Error creating sample file: {e}")
            return None


def interactive_bulk_import():
    """Interactive bulk import function for main menu"""
    print("🔄 BULK WEBSITE IMPORTER")
    print("=" * 50)
    
    importer = BulkWebsiteImporter()
    
    # Menu options
    print("1. Import from existing file")
    print("2. Create sample import file")
    print("3. Back to main menu")
    print()
    
    choice = input("Select option (1-3): ").strip()
    
    if choice == "1":
        # Import from file
        file_path = input("Enter path to import file: ").strip()
        
        if not file_path:
            print("No file path provided.")
            return
        
        # Get import options
        print("\nIMPORT OPTIONS:")
        auto_enable = input("Enable imported websites automatically? (Y/n): ").strip().lower()
        auto_enable = auto_enable != 'n'
        
        skip_duplicates = input("Skip duplicate websites? (Y/n): ").strip().lower()
        skip_duplicates = skip_duplicates != 'n'
        
        print(f"\nStarting import...")
        print(f"Auto-enable: {auto_enable}")
        print(f"Skip duplicates: {skip_duplicates}")
        print()
        
        # Perform import
        result = importer.import_from_file(file_path, auto_enable, skip_duplicates)
        
        # Show results
        if 'error' in result:
            print(f"❌ Import failed: {result['error']}")
        else:
            print(f"\n📊 IMPORT SUMMARY:")
            print(f"  ✅ Imported: {result['imported']} websites")
            print(f"  ⏭️ Skipped: {result['skipped']} websites")
            print(f"  ❌ Errors: {result['errors']} websites")
            print(f"  📋 Total processed: {result['total_processed']} lines")
            
            if result['imported'] > 0:
                print(f"\n🎉 Successfully imported {result['imported']} new websites!")
                print("You can now use them in your scraping and crawling operations.")
            
            # Show detailed results if there were errors
            if result['errors'] > 0 or result['skipped'] > 0:
                show_details = input("\nShow detailed results? (y/N): ").strip().lower()
                if show_details == 'y':
                    print("\nDETAILED RESULTS:")
                    print("-" * 40)
                    for res in result['results']:
                        status_icon = "✅" if res['status'] == 'imported' else "⏭️" if res['status'] == 'skipped' else "❌"
                        print(f"Line {res['line']}: {status_icon} {res['name']} - {res['reason']}")
    
    elif choice == "2":
        # Create sample file
        file_name = input("Sample file name (default: sample_websites_import.txt): ").strip()
        if not file_name:
            file_name = "sample_websites_import.txt"
        
        sample_path = importer.create_sample_import_file(file_name)
        if sample_path:
            print(f"✅ Created sample file: {sample_path}")
            print("Edit this file with your websites and then import it.")
        else:
            print("❌ Failed to create sample file.")
    
    elif choice == "3":
        return
    
    else:
        print("Invalid option.")
    
    input("\nPress Enter to continue...")


if __name__ == "__main__":
    import sys
    
    # Check for command line arguments
    if len(sys.argv) > 1:
        # Command line mode
        file_path = sys.argv[1]
        auto_enable = True
        skip_duplicates = True
        
        # Parse additional arguments
        if len(sys.argv) > 2:
            for arg in sys.argv[2:]:
                if arg.lower() == '--disable':
                    auto_enable = False
                elif arg.lower() == '--allow-duplicates':
                    skip_duplicates = False
        
        print(f"🔄 Command Line Bulk Import")
        print(f"File: {file_path}")
        print(f"Auto-enable: {auto_enable}")
        print(f"Skip duplicates: {skip_duplicates}")
        print("=" * 50)
        
        importer = BulkWebsiteImporter()
        result = importer.import_from_file(file_path, auto_enable, skip_duplicates)
        
        if 'error' in result:
            print(f"❌ Error: {result['error']}")
            sys.exit(1)
        else:
            print(f"\n📊 IMPORT COMPLETE:")
            print(f"  ✅ Imported: {result['imported']} websites")
            print(f"  ⏭️ Skipped: {result['skipped']} websites")
            print(f"  ❌ Errors: {result['errors']} websites")
            
            if result['imported'] > 0:
                print(f"\n🎉 Successfully imported {result['imported']} new websites!")
                print("You can now use them in your scraping operations.")
            
            sys.exit(0)
    else:
        # Interactive mode
        interactive_bulk_import()
