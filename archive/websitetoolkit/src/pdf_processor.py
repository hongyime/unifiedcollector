"""
Unified Website Toolkit - PDF Processor
PDF detection, download, and conversion to PNG images
"""
import os
from logger_config import setup_logger

logger = setup_logger(__name__)

import asyncio
from typing import List, Dict, Set, Optional, Tuple, Any
from urllib.parse import urlparse
from pathlib import Path
import hashlib
from datetime import datetime
import tempfile
import shutil

# Dynamic path resolution
TOOLKIT_ROOT = Path(__file__).resolve().parent
DEFAULT_DOWNLOADS = TOOLKIT_ROOT / "downloads"
DEFAULT_DATA = TOOLKIT_ROOT / "data"

# Optional async and PDF dependencies
try:
    import aiohttp
    import aiofiles
    ASYNC_AVAILABLE = True
except ImportError:
    ASYNC_AVAILABLE = False
    aiohttp = None
    aiofiles = None

try:
    import fitz  # PyMuPDF
    from pdf2image import convert_from_bytes
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    fitz = None
    convert_from_bytes = None

try:
    from config import DOWNLOADS_DIR, DATA_DIR
except ImportError:
    DOWNLOADS_DIR = str(DEFAULT_DOWNLOADS)
    DATA_DIR = str(DEFAULT_DATA)

from utils import get_safe_filename, calculate_content_hash, get_domain_name


class PDFProcessor:
    """PDF detection, download and conversion to images"""
    
    def __init__(self, 
                 max_file_size: int = 50 * 1024 * 1024,  # 50MB max
                 timeout: int = 60,
                 max_pages_per_pdf: int = 100):
        """Initialize PDF processor
        
        Args:
            max_file_size: Maximum PDF file size to download (bytes)
            timeout: Request timeout in seconds
            max_pages_per_pdf: Maximum pages to convert per PDF
        """
        self.max_file_size = max_file_size
        self.timeout = timeout
        self.max_pages_per_pdf = max_pages_per_pdf
        
        # Check if PDF processing is available
        if not PDF_AVAILABLE:
            logger.warning("WARNING: PDF processing dependencies not available (PyMuPDF, pdf2image)")
        
        # Track processed PDFs to avoid duplicates
        self.processed_hashes = set()
        self.load_processed_hashes()
        
        # Statistics
        self.stats = {
            'pdfs_found': 0,
            'pdfs_downloaded': 0,
            'pdfs_converted': 0,
            'pages_converted': 0,
            'total_size_downloaded': 0,
            'failed_downloads': 0,
            'failed_conversions': 0,
            'skipped_duplicates': 0,
            'start_time': None,
            'end_time': None
        }
    
    def load_processed_hashes(self):
        """Load previously processed PDF hashes"""
        try:
            from db_manager import get_db_manager
            db = get_db_manager()
            self.processed_hashes = set(db.get_all_hashes('pdf'))
        except Exception as e:
            logger.warning(f"WARNING: Error loading PDF hashes from DB: {e}")
            self.processed_hashes = set()
    
    def save_processed_hashes(self):
        """No-op, as hashes are saved instantly in DB now"""
        pass
    
    def save_hash(self, hash_id: str):
        try:
            from db_manager import get_db_manager
            db = get_db_manager()
            db.add_hash(hash_id, 'pdf', datetime.now().isoformat())
            self.processed_hashes.add(hash_id)
        except Exception:
            pass
    
    def is_pdf_url(self, url: str) -> bool:
        """Check if URL likely points to a PDF"""
        try:
            parsed = urlparse(url.lower())
            path = parsed.path.lower()
            
            # Direct PDF extension check
            if path.endswith('.pdf'):
                return True
            
            # Check query parameters for PDF indicators
            query = parsed.query.lower()
            if 'pdf' in query or 'filetype=pdf' in query:
                return True
            
            # Check for common PDF URL patterns
            pdf_patterns = [
                '/pdf/',
                '/documents/',
                '/files/',
                '/downloads/',
                '/assets/pdf',
                '.pdf?',
                'type=pdf'
            ]
            
            full_url = url.lower()
            return any(pattern in full_url for pattern in pdf_patterns)
            
        except Exception:
            return False
    
    async def download_pdf(self, url: str, website_name: str) -> Optional[str]:
        """Download PDF file and return local path
        
        Args:
            url: PDF URL to download
            website_name: Name of the website (for organization)
            
        Returns:
            Local file path if successful, None if failed
        """
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as session:
                
                # First, check content type and size
                async with session.head(url) as response:
                    if response.status != 200:
                        return None
                    
                    content_type = response.headers.get('content-type', '').lower()
                    if 'pdf' not in content_type and not self.is_pdf_url(url):
                        return None
                    
                    content_length = response.headers.get('content-length')
                    if content_length and int(content_length) > self.max_file_size:
                        logger.info(f"PDF: Skipping {url} - too large ({content_length} bytes)")
                        return None
                
                # Download the PDF using chunked reading
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    
                    # Create download directory
                    safe_website_name = get_safe_filename(website_name)
                    pdf_dir = os.path.join(DOWNLOADS_DIR, safe_website_name, 'pdfs')
                    os.makedirs(pdf_dir, exist_ok=True)
                    
                    # Generate safe filename
                    parsed_url = urlparse(url)
                    original_filename = os.path.basename(parsed_url.path)
                    if not original_filename.endswith('.pdf'):
                        original_filename = f"{original_filename}.pdf"
                    
                    safe_filename = get_safe_filename(original_filename)
                    if not safe_filename.endswith('.pdf'):
                        safe_filename += '.pdf'
                    
                    # Ensure unique filename
                    counter = 1
                    base_path = os.path.join(pdf_dir, safe_filename)
                    file_path = base_path
                    
                    while os.path.exists(file_path):
                        name, ext = os.path.splitext(safe_filename)
                        file_path = os.path.join(pdf_dir, f"{name}_{counter}{ext}")
                        counter += 1

                    # Write to file in chunks
                    hasher = hashlib.sha256()
                    downloaded_size = 0
                    is_pdf = False
                    first_chunk = True
                    
                    async with aiofiles.open(file_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            if first_chunk:
                                # Check magic bytes
                                if not chunk.startswith(b'%PDF-'):
                                    break
                                is_pdf = True
                                first_chunk = False
                            
                            hasher.update(chunk)
                            downloaded_size += len(chunk)
                            if self.max_file_size and downloaded_size > self.max_file_size:
                                break
                            await f.write(chunk)

                    if not is_pdf or (self.max_file_size and downloaded_size > self.max_file_size):
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        return None
                    
                    # Calculate hash to check for duplicates — atomic DB claim closes cross-process race
                    content_hash = hasher.hexdigest()
                    from db_manager import get_db_manager
                    if content_hash in self.processed_hashes or not get_db_manager().claim_hash_atomic(content_hash, 'pdf'):
                        self.stats['skipped_duplicates'] += 1
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        return None
                    self.processed_hashes.add(content_hash)
                    
                    # Update statistics
                    self.stats['pdfs_downloaded'] += 1
                    self.stats['total_size_downloaded'] += downloaded_size
                    
                    logger.info(f"PDF: Downloaded {url} -> {os.path.basename(file_path)}")
                    return file_path
        
        except Exception as e:
            logger.error(f"ERROR: Failed to download PDF {url}: {e}")
            self.stats['failed_downloads'] += 1
            return None
    
    async def convert_pdf_to_images(self, pdf_path: str, website_name: str) -> List[str]:
        """Convert PDF pages to PNG images
        
        Args:
            pdf_path: Path to the PDF file
            website_name: Name of the website (for organization)
            
        Returns:
            List of generated image file paths
        """
        image_paths = []
        
        try:
            # Check if PyMuPDF (fitz) is available
            try:
                import fitz  # PyMuPDF
            except ImportError:
                logger.warning("WARNING: PyMuPDF not installed. Install with: pip install PyMuPDF")
                return await self._convert_pdf_with_alternatives(pdf_path, website_name)
            
            # Open PDF
            pdf_document = fitz.open(pdf_path)
            
            # Create output directory
            safe_website_name = get_safe_filename(website_name)
            pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]
            images_dir = os.path.join(DOWNLOADS_DIR, safe_website_name, 'pdf_images', pdf_basename)
            os.makedirs(images_dir, exist_ok=True)
            
            # Convert pages (limit to max_pages_per_pdf)
            total_pages = min(len(pdf_document), self.max_pages_per_pdf)
            
            for page_num in range(total_pages):
                try:
                    page = pdf_document.load_page(page_num)
                    
                    # Render page as image (300 DPI for good quality)
                    mat = fitz.Matrix(300/72, 300/72)  # 300 DPI scaling
                    pix = page.get_pixmap(matrix=mat)
                    
                    # Save as PNG
                    image_filename = f"page_{page_num + 1:03d}.png"
                    image_path = os.path.join(images_dir, image_filename)
                    
                    pix.save(image_path)
                    image_paths.append(image_path)
                    
                    self.stats['pages_converted'] += 1
                    
                except Exception as e:
                    logger.error(f"WARNING: Failed to convert page {page_num + 1}: {e}")
                    continue
            
            pdf_document.close()
            self.stats['pdfs_converted'] += 1
            
            logger.info(f"PDF: Converted {total_pages} pages from {os.path.basename(pdf_path)}")
            return image_paths
        
        except Exception as e:
            logger.error(f"ERROR: Failed to convert PDF {pdf_path}: {e}")
            self.stats['failed_conversions'] += 1
            return []
    
    async def _convert_pdf_with_alternatives(self, pdf_path: str, website_name: str) -> List[str]:
        """Fallback PDF conversion using alternative methods"""
        try:
            # Try using Pillow with pdf2image
            try:
                from pdf2image import convert_from_path
            except ImportError:
                logger.warning("WARNING: pdf2image not installed. Install with: pip install pdf2image")
                return []
            
            # Create output directory
            safe_website_name = get_safe_filename(website_name)
            pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]
            images_dir = os.path.join(DOWNLOADS_DIR, safe_website_name, 'pdf_images', pdf_basename)
            os.makedirs(images_dir, exist_ok=True)
            
            # Convert PDF to images
            pages = convert_from_path(
                pdf_path, 
                dpi=300, 
                first_page=1, 
                last_page=min(self.max_pages_per_pdf, 999)
            )
            
            image_paths = []
            for i, page in enumerate(pages):
                image_filename = f"page_{i + 1:03d}.png"
                image_path = os.path.join(images_dir, image_filename)
                
                page.save(image_path, 'PNG')
                image_paths.append(image_path)
                
                self.stats['pages_converted'] += 1
            
            self.stats['pdfs_converted'] += 1
            logger.info(f"PDF: Converted {len(pages)} pages from {os.path.basename(pdf_path)}")
            return image_paths
        
        except Exception as e:
            logger.error(f"ERROR: Alternative PDF conversion failed for {pdf_path}: {e}")
            self.stats['failed_conversions'] += 1
            return []
    
    async def process_pdf_url(self, url: str, website_name: str) -> Dict[str, Any]:
        """Complete PDF processing: download and convert
        
        Args:
            url: PDF URL to process
            website_name: Name of the website
            
        Returns:
            Dictionary with processing results
        """
        self.stats['pdfs_found'] += 1
        
        try:
            # Download PDF
            pdf_path = await self.download_pdf(url, website_name)
            if not pdf_path:
                return {
                    'success': False,
                    'url': url,
                    'error': 'Download failed',
                    'pdf_path': None,
                    'image_paths': []
                }
            
            # Convert to images
            image_paths = await self.convert_pdf_to_images(pdf_path, website_name)
            
            return {
                'success': True,
                'url': url,
                'pdf_path': pdf_path,
                'image_paths': image_paths,
                'pages_converted': len(image_paths)
            }
        
        except Exception as e:
            return {
                'success': False,
                'url': url,
                'error': str(e),
                'pdf_path': None,
                'image_paths': []
            }
    
    async def process_pdf_urls(self, urls: List[str], website_name: str) -> Dict[str, Any]:
        """Process multiple PDF URLs concurrently
        
        Args:
            urls: List of PDF URLs to process
            website_name: Name of the website
            
        Returns:
            Combined processing results
        """
        self.stats['start_time'] = datetime.now().isoformat()
        
        # Filter for PDF URLs
        pdf_urls = [url for url in urls if self.is_pdf_url(url)]
        
        if not pdf_urls:
            return {
                'processed_pdfs': [],
                'total_pdfs': 0,
                'total_images': 0,
                'stats': self.stats.copy()
            }
        
        logger.info(f"PDF: Processing {len(pdf_urls)} PDF URLs for {website_name}")
        
        # Process PDFs with limited concurrency
        semaphore = asyncio.Semaphore(3)  # Limit concurrent downloads
        
        async def process_with_semaphore(url):
            async with semaphore:
                return await self.process_pdf_url(url, website_name)
        
        # Process all PDFs
        tasks = [process_with_semaphore(url) for url in pdf_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Compile results
        processed_pdfs = []
        total_images = 0
        
        for result in results:
            if isinstance(result, dict) and result.get('success'):
                processed_pdfs.append(result)
                total_images += len(result.get('image_paths', []))
        
        self.stats['end_time'] = datetime.now().isoformat()
        
        # Save processed hashes
        self.save_processed_hashes()
        
        return {
            'processed_pdfs': processed_pdfs,
            'total_pdfs': len(processed_pdfs),
            'total_images': total_images,
            'stats': self.stats.copy()
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """Get PDF processing statistics"""
        return self.stats.copy()
    
    def reset_stats(self):
        """Reset PDF processing statistics"""
        self.stats = {
            'pdfs_found': 0,
            'pdfs_downloaded': 0,
            'pdfs_converted': 0,
            'pages_converted': 0,
            'total_size_downloaded': 0,
            'failed_downloads': 0,
            'failed_conversions': 0,
            'skipped_duplicates': 0,
            'start_time': None,
            'end_time': None
        }


# Convenience functions
async def process_pdf_urls_for_website(urls: List[str], website_name: str) -> Dict[str, Any]:
    """Convenience function to process PDF URLs for a website"""
    processor = PDFProcessor()
    return await processor.process_pdf_urls(urls, website_name)

def install_pdf_dependencies():
    """Install required PDF processing dependencies"""
    try:
        import subprocess
        import sys
        
        packages = ['PyMuPDF', 'pdf2image']
        
        for package in packages:
            try:
                __import__(package.lower().replace('-', '_'))
                logger.info(f"✓ {package} is already installed")
            except ImportError:
                logger.info(f"Installing {package}...")
                subprocess.check_call([sys.executable, '-m', 'pip', 'install', package])
                logger.info(f"✓ {package} installed successfully")
        
        # Check for system dependencies for pdf2image
        logger.info("\nNOTE: pdf2image requires poppler-utils to be installed:")
        logger.info("- Windows: Download from https://github.com/oschwartz10612/poppler-windows")
        logger.info("- Ubuntu: sudo apt-get install poppler-utils")
        logger.info("- macOS: brew install poppler")
        
    except Exception as e:
        logger.error(f"ERROR: Failed to install PDF dependencies: {e}")
        return False
    
    return True