"""Remove lighting differences between two photographs of the same board.

Geometric alignment puts the pixels in the right place; this puts them on the
right scale. Without it, a cloud passing the skylight shifts every ROI's
brightness and the absolute thresholds in ``inspector.py`` stop meaning
anything.

The correction is deliberately a *global, robust, per-channel affine* map:

* global -- a local correction would happily "fix" the very patch where a
  component went missing, erasing the signal we are looking for;
* robust (median / MAD rather than mean / std) -- a few genuinely changed
  regions must not drag the estimate;
* affine (gain + offset) -- enough for exposure, aperture and lamp-ageing
  drift, and too rigid to absorb a real defect.
"""

from __future__ import annotations

import cv2
import numpy as np

EPS = 1e-6


def board_mask(image_bgr: np.ndarray) -> np.ndarray:
    """Coarse mask of the board itself, excluding the conveyor / background.

    Used only to keep the photometric estimate off the background, so a crude
    mask is fine. Falls back to "everything" when segmentation is unconvincing
    -- a wrong mask is worse than no mask.
    """
    h, w = image_bgr.shape[:2]
    full = np.full((h, w), 255, dtype=np.uint8)

    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]

    # A populated board is either coloured solder mask (high saturation) or
    # dark components (low value). Background tends to be neither.
    _, sat_mask = cv2.threshold(saturation, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark_mask = (value < 90).astype(np.uint8) * 255
    mask = cv2.bitwise_or(sat_mask, dark_mask)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return full

    largest = max(contours, key=cv2.contourArea)
    coverage = cv2.contourArea(largest) / float(h * w)

    # Too small => we probably segmented a component. Too large => the mask is
    # not telling us anything. Either way, use the full frame.
    if coverage < 0.15 or coverage > 0.985:
        return full

    out = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(out, [largest], -1, 255, thickness=cv2.FILLED)
    return out


def _robust_stats(channel: np.ndarray, mask: np.ndarray | None) -> tuple[float, float]:
    """Median and median-absolute-deviation of a channel inside ``mask``."""
    values = channel[mask > 0] if mask is not None else channel.ravel()
    if values.size < 64:
        values = channel.ravel()
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    return median, max(mad, 1.0)


def match_levels(
    source_bgr: np.ndarray,
    reference_bgr: np.ndarray,
    mask: np.ndarray | None = None,
    max_gain: float = 2.2,
) -> tuple[np.ndarray, dict[str, float]]:
    """Map ``source_bgr`` onto the reference's exposure, channel by channel.

    Returns the corrected image and a small report describing how large the
    correction was -- surfaced in the UI so an operator can see when the
    lighting has drifted far enough to be worth fixing at the fixture.
    """
    corrected = np.empty_like(source_bgr)
    gains: list[float] = []
    offsets: list[float] = []

    for c in range(3):
        src_median, src_mad = _robust_stats(source_bgr[:, :, c], mask)
        ref_median, ref_mad = _robust_stats(reference_bgr[:, :, c], mask)

        gain = float(np.clip(ref_mad / (src_mad + EPS), 1.0 / max_gain, max_gain))
        offset = ref_median - gain * src_median

        channel = source_bgr[:, :, c].astype(np.float32) * gain + offset
        corrected[:, :, c] = np.clip(channel, 0, 255).astype(np.uint8)

        gains.append(gain)
        offsets.append(offset)

    report = {
        "gain": float(np.mean(gains)),
        "offset": float(np.mean(offsets)),
        "max_channel_gain": float(np.max(gains)),
        # 0 = identical exposure, 1 = heavily corrected.
        "drift": float(
            np.clip(
                abs(float(np.mean(gains)) - 1.0) * 1.6
                + abs(float(np.mean(offsets))) / 90.0,
                0.0,
                1.0,
            )
        ),
    }
    return corrected, report
