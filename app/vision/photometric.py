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


def match_levels_around(
    reference_ring: np.ndarray,
    test_ring: np.ndarray,
    test_patch: np.ndarray,
    max_gain: float = 2.6,
    min_pixels: int = 60,
) -> tuple[np.ndarray, float]:
    """Correct one patch for the lighting on its own piece of board.

    The whole-image exposure match handles a lamp that changed or a camera
    that re-metered, because those move every pixel together. It cannot touch
    the two things that actually break a hand-held photo: somebody leaning
    over the board, and a flash bouncing off the solder mask. Both are local.
    A shadow lying across one corner leaves the rest of the frame correct, so
    a global correction averages it away to nearly nothing and the parts
    inside the shadow are still far darker than the same parts in the
    reference. Measured on hand-held photographs, that alone cost half the
    precision, and almost every false alarm was a large component: the bigger
    the part, the more of the shadow or the glare it covers.

    The ring of board immediately around a component is the fix, because it is
    the same bare board in both photographs whether or not the part is fitted.
    Any difference there is light, not absence, so the ratio between the two
    rings is an honest estimate of what the light did to this part, measured
    without ever looking at the part itself. That last point is what makes it
    safe: a missing component cannot flatter its own correction.

    Returns the corrected patch and the gain that was applied.
    """
    if reference_ring.size < min_pixels or test_ring.size < min_pixels:
        return test_patch, 1.0

    corrected = np.empty_like(test_patch)
    gains: list[float] = []
    for c in range(3):
        ref_level, ref_spread = _robust_stats(reference_ring[:, c], None)
        test_level, test_spread = _robust_stats(test_ring[:, c], None)

        # Gain from the spread, not the level. A reflection does not simply
        # brighten what is under it, it flattens it: the whole patch moves up
        # towards white and the difference between a component and the board
        # it sits on shrinks. Rescaling by brightness alone darkens the patch
        # back down and leaves it just as flat, which still reads as a part
        # that has lost its texture, which is what a missing part looks like.
        # Matching the spread as well restores the contrast the light took
        # away, and the offset then puts the patch back at the right level.
        gain = float(np.clip(
            (ref_spread + EPS) / (test_spread + EPS), 1.0 / max_gain, max_gain
        ))
        offset = ref_level - gain * test_level

        corrected[:, :, c] = np.clip(
            test_patch[:, :, c].astype(np.float32) * gain + offset, 0, 255
        ).astype(np.uint8)
        gains.append(gain)
    return corrected, float(np.mean(gains))


def light_anomaly_mask(
    reference_bgr: np.ndarray,
    test_bgr: np.ndarray,
    bare_board: np.ndarray | None = None,
    ratio: float = 1.30,
    blur_fraction: float = 0.03,
) -> np.ndarray:
    """Where the light on the test photo differs sharply from the reference.

    Built to keep a reflection from setting the exposure for the whole board.
    A highlight is a small patch of very bright pixels, and the whole-image
    statistics are not blind to it: it drags the median and the spread, so the
    correction computed from them comes out wrong everywhere else.

    ``bare_board`` is a mask of the places with no component on them, and it
    is not optional in spirit. Without it this function detects its own
    undoing: lift a large dark chip off a board and the bare pad underneath
    is far brighter than the chip was, which is precisely the signature of a
    highlight. Measured, that mistook eleven real removals for reflections.
    Illumination is a property of the light, so it is measured only where the
    board is bare, and interpolated across the components, which is also the
    only place the answer is trustworthy.

    Both images are blurred heavily first, so what is compared is the light
    falling on the board rather than anything printed or mounted on it. The
    comparison is against how the light changed everywhere else rather than
    against the reference directly, because the camera is free to have
    metered the scene differently, and that moves the whole frame together.
    """
    if reference_bgr.shape[:2] != test_bgr.shape[:2]:
        return np.zeros(reference_bgr.shape[:2], np.uint8)

    sigma = max(3.0, blur_fraction * max(reference_bgr.shape[:2]))
    ref_gray = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    test_gray = cv2.cvtColor(test_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)

    if bare_board is None:
        valid = np.ones(ref_gray.shape, np.float32)
    else:
        valid = (bare_board > 0).astype(np.float32)
        if valid.mean() < 0.05:
            return np.zeros(ref_gray.shape, np.uint8)

    # Normalised convolution: blur only the bare board and divide by the blur
    # of the mask, which spreads the surrounding illumination across the
    # components instead of letting them contribute to it.
    def field(image: np.ndarray) -> np.ndarray:
        num = cv2.GaussianBlur(image * valid, (0, 0), sigma)
        den = cv2.GaussianBlur(valid, (0, 0), sigma)
        return num / np.maximum(den, 1e-3)

    light = (field(test_gray) + EPS) / (field(ref_gray) + EPS)
    baseline = float(np.median(light[valid > 0])) if valid.any() else 1.0
    return ((light >= ratio * max(baseline, EPS)) * 255).astype(np.uint8)


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
