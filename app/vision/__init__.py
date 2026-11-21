"""Computer-vision layer.

Pure functions over NumPy arrays with no knowledge of HTTP, storage or the
UI, so the whole engine can be exercised head-less from ``tools/evaluate.py``.
"""

from app.vision.inspector import InspectionResult, PartVerdict, crop_pair, inspect
from app.vision.registration import Alignment, align, warp_to_reference
from app.vision.suggest import empty_box_fractions, suggest_parts

__all__ = [
    "Alignment",
    "InspectionResult",
    "PartVerdict",
    "align",
    "crop_pair",
    "inspect",
    "empty_box_fractions",
    "suggest_parts",
    "warp_to_reference",
]
