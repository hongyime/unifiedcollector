import pytest
import asyncio
from unittest.mock import patch, MagicMock
import os
import importlib.util

if importlib.util.find_spec('media_processor') is None:
    pytest.skip('legacy media_processor module not available in current architecture', allow_module_level=True)

@pytest.mark.asyncio
async def test_media_processor_process_static():
    from media_processor import media_processor
    
    with patch('os.path.exists', return_value=True), \
         patch('media_processor.Image.open') as mock_open:
        
        mock_img = MagicMock()
        mock_img.width = 100
        mock_img.height = 100
        mock_img.mode = 'RGB'
        mock_img.__enter__.return_value = mock_img
        mock_open.return_value = mock_img
        
        results = await media_processor.process('test.jpg', 'image/jpeg', {})
        assert len(results) == 1
        assert results[0] == 'test.jpg'

@pytest.mark.asyncio
async def test_media_processor_process_missing_file():
    from media_processor import media_processor
    
    with patch('os.path.exists', return_value=False):
        results = await media_processor.process('missing.jpg', 'image/jpeg', {})
        assert len(results) == 0
