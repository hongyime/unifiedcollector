from __future__ import annotations

import sys
import warnings
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

warnings.filterwarnings(
    "ignore",
    message=r".*doesn't match a supported version!.*",
    category=Warning,
)
