import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False


class PDFProcessor:

    def __init__(self, dpi: int = 150, max_pages: int = 100, max_file_size: int = 50 * 1024 * 1024):
        self._dpi = dpi
        self._max_pages = max_pages
        self._max_file_size = max_file_size

    @property
    def available(self) -> bool:
        return HAS_PYMUPDF

    def convert_to_images(self, pdf_bytes: bytes) -> list[tuple[bytes, int]]:
        if not HAS_PYMUPDF:
            logger.debug("PyMuPDF not installed, skipping PDF conversion")
            return []

        if len(pdf_bytes) > self._max_file_size:
            logger.debug("PDF too large (%d bytes), skipping", len(pdf_bytes))
            return []

        results: list[tuple[bytes, int]] = []
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            page_count = min(len(doc), self._max_pages)

            for page_num in range(page_count):
                page = doc[page_num]
                zoom = self._dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                png_bytes = pix.tobytes("png")
                results.append((png_bytes, page_num))

            doc.close()
        except Exception as e:
            logger.error("PDF conversion failed: %s", e)

        return results

    @staticmethod
    def is_pdf_url(url: str) -> bool:
        lower = url.lower()
        if lower.endswith(".pdf"):
            return True
        if "filetype=pdf" in lower:
            return True
        return False

    @staticmethod
    def is_pdf_content_type(content_type: str) -> bool:
        return "application/pdf" in content_type.lower()
