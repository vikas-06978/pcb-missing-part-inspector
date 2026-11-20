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


def gradient_magnitude(gray: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def _zero_mean_correlation(a: np.ndarray, b: np.ndarray) -> float:
    av = a.astype(np.float32).ravel()
    bv = b.astype(np.float32).ravel()
    av = av - av.mean()
    bv = bv - bv.mean()
    denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
    if denom < 1e-6:
        # One of the patches is perfectly flat. There is no structure to agree
        # or disagree about, so this channel abstains rather than guesses.
        return 1.0
    return float(np.clip(float(np.dot(av, bv)) / denom, -1.0, 1.0))


def patch_correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-mean normalised correlation of two same-sized patches.

    Used where the patches are already aligned, so the full template search of
    :func:`best_offset_ncc` would only add cost.
    """
    return _zero_mean_correlation(to_gray(a), to_gray(b))


def edge_correlation(reference_gray: np.ndarray, test_gray: np.ndarray) -> float:
    """Correlation between the two patches' gradient-magnitude maps.

    This replaced a dilated-Canny IoU, which measured well on crisp photos and
    then collapsed the moment the lens went slightly soft: blur pushes
    gradients under the Canny threshold, the test edge map empties out, and a
    perfectly present component reads as absent. On the evaluation set that
    single failure mode produced most of the false alarms.

    Correlating gradient magnitudes fixes it, because blur scales the whole
    gradient field down roughly uniformly and correlation is invariant to
    scale. What it still punishes is structure appearing in *different*
    places -- which is exactly what a removal looks like.
    """
    return _zero_mean_correlation(
        gradient_magnitude(reference_gray), gradient_magnitude(test_gray)
    )


def edge_density(gray: np.ndarray) -> float:
    """Fraction of pixels Canny calls an edge. Reported for diagnostics only."""
    median = float(np.median(gray))
    low = int(max(0, 0.67 * median))
    high = int(min(255, 1.33 * median))
    if high <= low:
        low, high = 50, 150
    edges = cv2.Canny(gray, low, high)
    return float(np.count_nonzero(edges)) / max(edges.size, 1)


def gradient_energy(gray: np.ndarray) -> float:
    """Mean gradient magnitude -- a scale-free proxy for "how much is here"."""
    return float(gradient_magnitude(gray).mean())


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


def _chroma_bins(pixel_count: int) -> int:
    """Histogram resolution scaled to how many samples the patch actually has.

    A 22x40 chip resistor gives 880 samples. Spread over 24x24 bins that is
    1.5 samples per bin, and the resulting histogram measures sampling noise
    rather than colour -- which is how a part that was plainly still there
    ended up with a colour distance of 0.7.
    """
    return int(np.clip(round(np.sqrt(pixel_count) / 4.0), 8, ANALYSIS.chroma_bins))


def _chroma_histogram(image_bgr: np.ndarray, bins: int) -> np.ndarray:
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)
    hist = cv2.calcHist([lab], [1, 2], None, [bins, bins], [0, 256, 0, 256])

    # Smooth before comparing. Without it, two photographs of the *same* patch
    # land their samples in neighbouring bins and the histograms barely
    # overlap -- the comparison then measures quantisation, not colour.
    hist = cv2.GaussianBlur(hist, (0, 0), ANALYSIS.chroma_smoothing)

    total = float(hist.sum())
    if total > 0:
        hist /= total
    return hist


def colour_distance(reference_bgr: np.ndarray, test_bgr: np.ndarray) -> float:
    """Bhattacharyya distance between CIELAB chroma histograms (0 = identical).

    Chroma (a*, b*) only: lightness is dropped so a brighter or dimmer
    exposure does not register as a change, while a patch turning into the
    colour of bare solder mask does.

    This started out as a hue/saturation histogram and was wrong in a way that
    only the evaluation set revealed -- it returned ~1.0 for *identical*
    patches. Hue is undefined for near-neutral colours, and a PCB is mostly
    near-neutral: black IC bodies, white connectors, grey pads. Those hues
    were noise, so two photos of the same part shared almost no bins. CIELAB
    chroma degrades gracefully instead: neutrals simply cluster near the
    origin, where they belong, and still overlap.
    """
    if reference_bgr.ndim == 2:
        reference_bgr = cv2.cvtColor(reference_bgr, cv2.COLOR_GRAY2BGR)
    if test_bgr.ndim == 2:
        test_bgr = cv2.cvtColor(test_bgr, cv2.COLOR_GRAY2BGR)

    bins = _chroma_bins(reference_bgr.shape[0] * reference_bgr.shape[1])
    ref_hist = _chroma_histogram(reference_bgr, bins)
    test_hist = _chroma_histogram(test_bgr, bins)

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


def saturated_fraction(patch_bgr: np.ndarray, level: int = 247) -> float:
    """Fraction of a patch that is blown out to white.

    A reflection off the solder mask does not darken or distort the component
    underneath, it deletes it: every channel pegs at the top of the range and
    nothing about the pixel depends any more on what was there. No comparison
    can recover that, so the honest use of this number is to decline the
    measurement rather than to correct it.
    """
    if patch_bgr.size == 0:
        return 0.0
    if patch_bgr.ndim == 2:
        return float((patch_bgr >= level).mean())
    return float((patch_bgr.min(axis=2) >= level).mean())
