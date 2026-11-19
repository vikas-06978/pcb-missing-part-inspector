"""Low-level image measurements used as evidence channels.

Each function answers one narrow question about a pair of image patches. None
of them decides anything on its own -- ``inspector.py`` fuses them. Keeping the
measurements separate is what makes a verdict explainable to an operator:
"no template match, edges gone, colour is now bare board".
"""

from __future__ import annotations

import cv2
import numpy as np

from app.config import ANALYSIS

EPS = 1e-9


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def ramp(value: float, low: float, high: float) -> float:
    """Linear 0..1 ramp, clamped at both ends."""
    if high <= low:
        return 0.0
    return float(np.clip((value - low) / (high - low), 0.0, 1.0))


def inv_ramp(value: float, low: float, high: float) -> float:
    """1 at ``low`` or below, 0 at ``high`` or above."""
    return 1.0 - ramp(value, low, high)


def ensure_min_size(patch: np.ndarray, min_px: int = ANALYSIS.min_patch_px) -> np.ndarray:
    """Upscale tiny ROIs so window-based statistics stay meaningful.

    A 9x6 px patch has no room for an 11x11 SSIM window and Canny on it is
    noise. Upscaling does not create information, but it makes the measurement
    numerically stable and keeps the thresholds comparable across part sizes.
    """
    h, w = patch.shape[:2]
    if h >= min_px and w >= min_px:
        return patch
    scale = max(min_px / max(h, 1), min_px / max(w, 1))
    new_size = (max(int(round(w * scale)), min_px), max(int(round(h * scale)), min_px))
    return cv2.resize(patch, new_size, interpolation=cv2.INTER_CUBIC)


# --------------------------------------------------------------------------
# Structural similarity
# --------------------------------------------------------------------------


def ssim(a: np.ndarray, b: np.ndarray, sigma: float = 1.5) -> float:
    """Mean SSIM between two same-sized grayscale patches.

    Implemented directly (Wang et al. 2004, Gaussian-weighted variant) rather
    than pulling in scikit-image: it is ~25 lines, removes a dependency, and
    lets us control the window size for small ROIs.
    """
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)

    af = a.astype(np.float32)
    bf = b.astype(np.float32)

    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2

    mu_a = cv2.GaussianBlur(af, (0, 0), sigma)
    mu_b = cv2.GaussianBlur(bf, (0, 0), sigma)

    mu_a2 = mu_a * mu_a
    mu_b2 = mu_b * mu_b
    mu_ab = mu_a * mu_b

    sigma_a2 = cv2.GaussianBlur(af * af, (0, 0), sigma) - mu_a2
    sigma_b2 = cv2.GaussianBlur(bf * bf, (0, 0), sigma) - mu_b2
    sigma_ab = cv2.GaussianBlur(af * bf, (0, 0), sigma) - mu_ab

    numerator = (2.0 * mu_ab + c1) * (2.0 * sigma_ab + c2)
    denominator = (mu_a2 + mu_b2 + c1) * (sigma_a2 + sigma_b2 + c2)

    return float(np.clip((numerator / (denominator + EPS)).mean(), -1.0, 1.0))


# --------------------------------------------------------------------------
# Template matching with sub-window search
# --------------------------------------------------------------------------


def best_offset_ncc(
    template: np.ndarray,
    search_region: np.ndarray,
    anchor: tuple[int, int],
) -> tuple[float, int, int]:
    """Find where ``template`` sits inside ``search_region``.

    ``anchor`` is where the template would land at zero displacement. Returns
    ``(peak_correlation, dx, dy)`` where the offsets are relative to that
    anchor. Normalised cross-correlation is used because it is invariant to
    affine brightness change -- the single most common nuisance on a line where
    someone walks past the lamp.
    """
    t = to_gray(template)
    s = to_gray(search_region)

    th, tw = t.shape[:2]
    sh, sw = s.shape[:2]

    if sh < th or sw < tw:
        pad_y = max(0, th - sh)
        pad_x = max(0, tw - sw)
        s = cv2.copyMakeBorder(
            s, 0, pad_y, 0, pad_x, cv2.BORDER_REPLICATE
        )
        sh, sw = s.shape[:2]

    if th < 3 or tw < 3:
        return 0.0, 0, 0

    result = cv2.matchTemplate(s, t, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    dx = int(max_loc[0] - anchor[0])
    dy = int(max_loc[1] - anchor[1])
    return float(max_val), dx, dy


# --------------------------------------------------------------------------
# Edge structure
# --------------------------------------------------------------------------


def canny_thresholds(reference_gray: np.ndarray, sigma: float = 0.33) -> tuple[int, int]:
    """Derive Canny thresholds from the *reference* patch only.

    Both patches are then edged with the same thresholds. Deriving them
    per-image would adapt the detector to the very change we are trying to
    measure, which quietly destroys the signal.
    """
    median = float(np.median(reference_gray))
    low = int(max(0, (1.0 - sigma) * median))
    high = int(min(255, (1.0 + sigma) * median))
    if high <= low:
        low, high = 50, 150
    return low, high


def edge_iou(
    reference_gray: np.ndarray,
    test_gray: np.ndarray,
    dilate_px: int = ANALYSIS.edge_dilate_px,
) -> tuple[float, float, float]:
    """Intersection-over-union of dilated edge maps.

    Returns ``(iou, reference_edge_density, test_edge_density)``.

    A component is a silhouette: body outline, lead edges, printed markings.
    Remove it and that structure is replaced by bare laminate and two pads.
    IoU of slightly dilated edges tolerates a pixel of jitter while still
    collapsing when the structure genuinely changes.
    """
    low, high = canny_thresholds(reference_gray)

    ref_edges = cv2.Canny(reference_gray, low, high)
    test_edges = cv2.Canny(test_gray, low, high)

    if dilate_px > 0:
        k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * dilate_px + 1, 2 * dilate_px + 1)
        )
        ref_d = cv2.dilate(ref_edges, k)
        test_d = cv2.dilate(test_edges, k)
    else:
        ref_d, test_d = ref_edges, test_edges

    ref_bin = ref_d > 0
    test_bin = test_d > 0

    union = np.count_nonzero(ref_bin | test_bin)
    intersection = np.count_nonzero(ref_bin & test_bin)

    total = ref_bin.size
    ref_density = float(np.count_nonzero(ref_edges)) / max(total, 1)
    test_density = float(np.count_nonzero(test_edges)) / max(total, 1)

    if union == 0:
        # Neither patch has structure (e.g. a flat painted area). That is not
        # evidence of a removal, so report perfect agreement.
        return 1.0, ref_density, test_density

    return float(intersection) / float(union), ref_density, test_density


def gradient_energy(gray: np.ndarray) -> float:
    """Mean gradient magnitude -- a scale-free proxy for "how much is here"."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return float(np.sqrt(gx * gx + gy * gy).mean())


def gradient_log_ratio(reference_gray: np.ndarray, test_gray: np.ndarray) -> float:
    """Symmetric |log| ratio of gradient energy.

    Symmetric on purpose: removing a matte black IC from a green board *lowers*
    texture, but removing a dark shield can *expose* bright, high-contrast
    pads and raise it. Either direction is evidence of change.
    """
    g_ref = gradient_energy(reference_gray)
    g_test = gradient_energy(test_gray)
    return float(abs(np.log((g_test + 1.0) / (g_ref + 1.0))))


# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------


def hsv_hist_distance(reference_bgr: np.ndarray, test_bgr: np.ndarray) -> float:
    """Bhattacharyya distance between hue/saturation histograms (0 = identical).

    Hue and saturation only -- value is deliberately dropped so that a brighter
    or dimmer exposure does not register as a change. What it does catch is the
    thing we care about: the patch turning into the colour of bare solder mask.
    """
    if reference_bgr.ndim == 2:
        reference_bgr = cv2.cvtColor(reference_bgr, cv2.COLOR_GRAY2BGR)
    if test_bgr.ndim == 2:
        test_bgr = cv2.cvtColor(test_bgr, cv2.COLOR_GRAY2BGR)

    ref_hsv = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2HSV)
    test_hsv = cv2.cvtColor(test_bgr, cv2.COLOR_BGR2HSV)

    bins = [ANALYSIS.hist_bins_h, ANALYSIS.hist_bins_s]
    ranges = [0, 180, 0, 256]

    ref_hist = cv2.calcHist([ref_hsv], [0, 1], None, bins, ranges)
    test_hist = cv2.calcHist([test_hsv], [0, 1], None, bins, ranges)

    cv2.normalize(ref_hist, ref_hist, 0, 1, cv2.NORM_MINMAX)
    cv2.normalize(test_hist, test_hist, 0, 1, cv2.NORM_MINMAX)

    return float(cv2.compareHist(ref_hist, test_hist, cv2.HISTCMP_BHATTACHARYYA))


# --------------------------------------------------------------------------
# Residual
# --------------------------------------------------------------------------


def normalised_residual(reference_gray: np.ndarray, test_gray: np.ndarray) -> float:
    """Mean absolute difference divided by the reference's own contrast.

    Dividing by the reference standard deviation makes the number comparable
    across a busy connector and a plain capacitor: a 12-grey-level difference
    is nothing on a textured IC and glaring on a flat pad field.
    """
    ref_f = reference_gray.astype(np.float32)
    test_f = test_gray.astype(np.float32)
    mad = float(np.abs(ref_f - test_f).mean())
    contrast = float(ref_f.std())
    return mad / (contrast + 8.0)


def mean_intensity_shift(reference_gray: np.ndarray, test_gray: np.ndarray) -> float:
    """Absolute difference of patch means, normalised to 0..1."""
    return float(abs(float(reference_gray.mean()) - float(test_gray.mean())) / 255.0)
