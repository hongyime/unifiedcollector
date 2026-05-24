import pytest
import numpy as np
import sys
from unittest.mock import patch, MagicMock
import os
import importlib.util

if importlib.util.find_spec('face_processor') is None:
    pytest.skip('legacy face_processor module not available in current architecture', allow_module_level=True)

sys.modules['face_recognition'] = MagicMock()

original_exists = os.path.exists
def mocked_exists(path):
    if 'shape_predictor' in str(path) or 'dlib_face_recognition' in str(path): return True
    return original_exists(path)

with patch('os.path.exists', side_effect=mocked_exists):
    from face_processor import FaceProcessor

@patch('os.path.exists')
@patch('face_processor.FaceProcessor._verify_models')
def test_face_processor_encode_no_faces(mock_verify, mock_exists):
    mock_exists.return_value = True
    processor = FaceProcessor()
    
    with patch('cv2.imread') as mock_imread, \
         patch('cv2.cvtColor') as mock_cvt, \
         patch('face_recognition.face_locations') as mock_locs:
        
        mock_imread.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_cvt.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_locs.return_value = [] # No faces
        
        results = processor.encode('test.jpg')
        assert len(results) == 0

@patch('os.path.exists')
@patch('face_processor.FaceProcessor._verify_models')
def test_face_processor_encode_success(mock_verify, mock_exists):
    mock_exists.return_value = True
    processor = FaceProcessor()
    
    with patch('cv2.imread') as mock_imread, \
         patch('cv2.cvtColor') as mock_cvt, \
         patch('face_recognition.face_locations') as mock_locs, \
         patch('face_recognition.face_encodings') as mock_encs:
        
        mock_imread.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_cvt.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_locs.return_value = [(0, 10, 10, 0)] # One face
        mock_encs.return_value = [np.zeros(128)] # One embedding
        
        results = processor.encode('test.jpg')
        assert len(results) == 1
        assert len(results[0].embedding) == 128
        assert results[0].bbox == (0, 10, 10, 0)
