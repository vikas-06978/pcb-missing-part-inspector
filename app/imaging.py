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

from app.config import MAX_DECODED_PIXELS, MIN_LONG_EDGE, WORKING_LONG_EDGE


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
            # Refuse absurd dimensions before any pixels are allocated. A
            # compressed file is allowed to be 40 MB, and a PNG that size can
            # describe hundreds of megapixels, which would be decoded into
            # memory before anything here got a say. Nothing this application
            # does needs more than a few: the first thing that happens to a
            # photograph is being scaled to the working size anyway.
            pixels = (img.size[0] or 0) * (img.size[1] or 0)
            if pixels > MAX_DECODED_PIXELS:
                raise ImageError(
                    f"That image is {img.size[0]}x{img.size[1]} pixels, which is "
                    "larger than this application will open. Export it at a "
                    "more usual size and try again."
                )
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            rgb = np.array(img)
    except ImageError:
        raise
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


def fit_to_reference(
    image: np.ndarray, reference_shape: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """Resize a test photo to the reference's pixel dimensions.

    Returns ``(image, content_mask)``. The mask is 255 wherever the result
    holds real photographed pixels and 0 on letterbox padding, so a photo of
    part of the board does not later claim to have shown the padding.

    Registration can absorb a scale change, but starting from the same raster
    size keeps the feature detector working at the same spatial frequency as
    it did on the reference, which measurably improves match counts.
    """
    ref_h, ref_w = reference_shape[:2]
    full = np.full((ref_h, ref_w), 255, dtype=np.uint8)
    if image.shape[0] == ref_h and image.shape[1] == ref_w:
        return image, full

    ref_aspect = ref_w / float(ref_h)
    aspect = image.shape[1] / float(image.shape[0])
    interp = cv2.INTER_AREA if image.shape[1] > ref_w else cv2.INTER_CUBIC

    # Aspect ratios within 2% are treated as the same framing (JPEG rounding,
    # a slightly different crop), so the raster is matched exactly.
    if abs(ref_aspect - aspect) / max(ref_aspect, 1e-6) < 0.02:
        return cv2.resize(image, (ref_w, ref_h), interpolation=interp), full

    # Different framing entirely -- most often a photo of only part of the
    # board. Hand it over untouched.
    #
    # The earlier version letterboxed it onto a reference-sized black canvas.
    # That was actively harmful: the padding is a large flat region, the
    # registration stage runs CLAHE, and CLAHE turns the sensor noise in flat
    # black into texture that the feature detector then tries to match.
    # Alignment quality on a 60% crop sat at 0.65 because of it, and the
    # reported coverage was wrong by a third.
    #
    # SIFT is scale-invariant and RANSAC does not care about image bounds, so
    # the honest move is to let the homography place the photo itself.
    return image, np.full(image.shape[:2], 255, dtype=np.uint8)


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
