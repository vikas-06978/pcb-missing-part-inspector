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


# --------------------------------------------------------------------------
# Sharpness
# --------------------------------------------------------------------------


def _focus_measure(gray: np.ndarray) -> float:
    """Variance of the Laplacian -- the standard cheap sharpness estimate.

    Median-filtered first because sensor noise is high-frequency too, and an
    unfiltered noisy photo reads as *sharper* than a clean one.
    """
    denoised = cv2.medianBlur(gray, 3)
    return float(cv2.Laplacian(denoised, cv2.CV_32F).var())


def match_sharpness(
    reference_bgr: np.ndarray,
    test_bgr: np.ndarray,
    tolerance: float = 0.92,
    max_sigma: float = 2.6,
) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """Blur whichever image is sharper until both are equally soft.

    The reference is a careful, well-focused shot taken once. Line photos are
    not: autofocus hunts, the board sits a millimetre higher in the fixture,
    the lens is cheap. Comparing a sharp patch against a soft one inflates
    every difference measure, and it does so worst on exactly the parts that
    are hardest anyway -- small, dark components ringed by bright solder pads,
    where blur bleeds the pads into the body.

    On the 80-board stress set this single failure mode produced *every*
    false alarm at conservative sensitivity: one 22x40 px chip resistor,
    flagged on boards where nothing at all had been removed.

    Equalising sharpness is a nuisance correction in exactly the same sense as
    equalising exposure. It throws away detail that one of the two images
    never had, which is the point: a comparison may only use information that
    both sides actually contain.
    """
    ref_gray = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2GRAY)
    test_gray = cv2.cvtColor(test_bgr, cv2.COLOR_BGR2GRAY)

    # The search runs on a downscaled copy: sharpness is a global property and
    # 23 Gaussian blurs of a full-resolution board would dominate the runtime.
    scale = min(1.0, 700.0 / max(ref_gray.shape[:2]))
    if scale < 1.0:
        size = (int(ref_gray.shape[1] * scale), int(ref_gray.shape[0] * scale))
        ref_small = cv2.resize(ref_gray, size, interpolation=cv2.INTER_AREA)
        test_small = cv2.resize(test_gray, size, interpolation=cv2.INTER_AREA)
    else:
        ref_small, test_small = ref_gray, test_gray

    focus_ref = _focus_measure(ref_small)
    focus_test = _focus_measure(test_small)

    report = {
        "reference_focus": focus_ref,
        "test_focus": focus_test,
        "blur_applied_to": 0.0,   # -1 = test blurred, +1 = reference blurred
        "sigma": 0.0,
    }

    if focus_ref <= 0 or focus_test <= 0:
        return reference_bgr, test_bgr, report

    if focus_test < focus_ref * tolerance:
        sharper, target, blur_reference = ref_small, focus_test, True
    elif focus_ref < focus_test * tolerance:
        sharper, target, blur_reference = test_small, focus_ref, False
    else:
        return reference_bgr, test_bgr, report

    best_sigma, best_error = 0.0, float("inf")
    for sigma in np.arange(0.3, max_sigma, 0.1):
        error = abs(_focus_measure(cv2.GaussianBlur(sharper, (0, 0), float(sigma))) - target)
        if error < best_error:
            best_sigma, best_error = float(sigma), error

    if best_sigma <= 0.0:
        return reference_bgr, test_bgr, report

    # The sigma was found on the downscaled copy; scale it back up.
    full_sigma = best_sigma / scale if scale > 0 else best_sigma
    report["sigma"] = round(full_sigma, 3)
    report["blur_applied_to"] = 1.0 if blur_reference else -1.0

    if blur_reference:
        return cv2.GaussianBlur(reference_bgr, (0, 0), full_sigma), test_bgr, report
    return reference_bgr, cv2.GaussianBlur(test_bgr, (0, 0), full_sigma), report
