"""Image I/O and normalisation at the boundary of the application.

Everything inside ``app.vision`` assumes BGR ``uint8`` arrays that are already
at working resolution. This module is the only place that deals with bytes,
EXIF orientation, odd colour profiles and resizing.
"""

from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from app.config import MIN_LONG_EDGE, WORKING_LONG_EDGE


class ImageError(ValueError):
    """Raised when an upload cannot be used as a board photograph."""


def decode_upload(data: bytes) -> np.ndarray:
    """Decode uploaded bytes into a BGR array, honouring EXIF orientation.

    Pillow is used for decoding rather than ``cv2.imdecode`` for one reason:
    phones write a rotation flag instead of rotating pixels, and OpenCV
    ignores it. A reference photographed in portrait and a test photographed
    the same way would otherwise arrive rotated 90 degrees apart.
    """
    if not data:
        raise ImageError("The uploaded file was empty.")

    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            rgb = np.array(img)
    except Exception as exc:  # noqa: BLE001 - surface a friendly message
        raise ImageError(
            "That file could not be read as an image. "
            "Use a JPEG, PNG, BMP, WEBP or TIFF photo."
        ) from exc

    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    if max(bgr.shape[:2]) < MIN_LONG_EDGE:
        raise ImageError(
            f"That image is only {bgr.shape[1]}x{bgr.shape[0]} pixels. "
            f"Use a photo at least {MIN_LONG_EDGE} px on the long edge so "
            "small components are resolvable."
        )

    return bgr


def to_working_size(image: np.ndarray, long_edge: int = WORKING_LONG_EDGE) -> np.ndarray:
    """Downscale so the long edge is ``long_edge``. Never upscales."""
    height, width = image.shape[:2]
    current = max(height, width)
    if current <= long_edge:
        return image
    scale = long_edge / float(current)
    size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def fit_to_reference(image: np.ndarray, reference_shape: tuple[int, int]) -> np.ndarray:
    """Resize a test photo to the reference's pixel dimensions.

    Registration can absorb a scale change, but starting from the same raster
    size keeps the feature detector working at the same spatial frequency as
    it did on the reference, which measurably improves match counts.
    """
    ref_h, ref_w = reference_shape[:2]
    if image.shape[0] == ref_h and image.shape[1] == ref_w:
        return image

    ref_aspect = ref_w / float(ref_h)
    aspect = image.shape[1] / float(image.shape[0])
    interp = cv2.INTER_AREA if image.shape[1] > ref_w else cv2.INTER_CUBIC

    # Aspect ratios within 2% are treated as the same framing (JPEG rounding,
    # a slightly different crop). Anything further apart is letterboxed rather
    # than stretched, because stretching invents geometry.
    if abs(ref_aspect - aspect) / max(ref_aspect, 1e-6) < 0.02:
        return cv2.resize(image, (ref_w, ref_h), interpolation=interp)

    scale = min(ref_w / float(image.shape[1]), ref_h / float(image.shape[0]))
    size = (max(1, int(round(image.shape[1] * scale))), max(1, int(round(image.shape[0] * scale))))
    resized = cv2.resize(image, size, interpolation=interp)

    canvas = np.zeros((ref_h, ref_w, 3), dtype=np.uint8)
    y0 = (ref_h - resized.shape[0]) // 2
    x0 = (ref_w - resized.shape[1]) // 2
    canvas[y0:y0 + resized.shape[0], x0:x0 + resized.shape[1]] = resized
    return canvas


def save_png(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 4])
    if not ok:  # pragma: no cover - encoder failure is not recoverable here
        raise ImageError("Failed to encode image.")
    path.write_bytes(buffer.tobytes())


def save_jpeg(path: Path, image: np.ndarray, quality: int = 92) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:  # pragma: no cover
        raise ImageError("Failed to encode image.")
    path.write_bytes(buffer.tobytes())


def load(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ImageError(f"Could not read image at {path}.")
    return image


def encode_png_bytes(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 4])
    if not ok:  # pragma: no cover
        raise ImageError("Failed to encode image.")
    return buffer.tobytes()
