"""Bring a test photograph into the reference photograph's coordinate frame.

The brief promises a fixed camera, so the two frames are *nearly* identical.
"Nearly" is the whole problem: a board that sits 4 px further left in the
fixture will make every single ROI comparison fail if we compare raw pixels.
Everything downstream assumes this module has removed that drift.

Strategy, in order of preference:

1. Feature matching (SIFT, falling back to ORB) + RANSAC homography, then a
   sanity check that the recovered transform is a plausible camera nudge
   rather than a wild fit to repeated solder-mask texture.
2. ECC intensity alignment (affine) on a downscaled pair -- slower, but it
   does not care about feature repeatability and handles low-texture boards.
3. Identity, with a loud warning on the result object.

The final quality score is deliberately *not* the inlier ratio. It is the
whole-board correlation after warping, because that is the quantity the rest
of the pipeline actually depends on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from app.config import REGISTRATION
from app.vision.metrics import to_gray


@dataclass
class Alignment:
    """Result of aligning a test frame onto the reference frame."""

    matrix: np.ndarray                 # 3x3, maps test pixels -> reference pixels
    method: str                        # 'sift' | 'orb' | 'ecc' | 'identity'
    inliers: int = 0
    total_matches: int = 0
    inlier_ratio: float = 0.0
    translation_px: float = 0.0
    rotation_deg: float = 0.0
    scale: float = 1.0
    board_correlation: float = 0.0     # NCC after warping, over the covered area
    quality: float = 0.0               # 0..1 confidence in the alignment
    coverage: float = 1.0              # fraction of the reference the photo shows
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "inliers": self.inliers,
            "total_matches": self.total_matches,
            "inlier_ratio": round(self.inlier_ratio, 4),
            "translation_px": round(self.translation_px, 2),
            "rotation_deg": round(self.rotation_deg, 3),
            "scale": round(self.scale, 4),
            "board_correlation": round(self.board_correlation, 4),
            "quality": round(self.quality, 4),
            "coverage": round(self.coverage, 4),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# Preprocessing
# --------------------------------------------------------------------------


def _prepare(image: np.ndarray) -> np.ndarray:
    """Grayscale + CLAHE.

    Local contrast equalisation matters here: a glossy board has a specular
    hotspot that moves with the slightest change, and global normalisation
    lets that hotspot dominate the feature detector.
    """
    gray = to_gray(image)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _detector():
    """Prefer SIFT; fall back to ORB if this OpenCV build lacks it."""
    if hasattr(cv2, "SIFT_create"):
        try:
            return cv2.SIFT_create(nfeatures=REGISTRATION.max_features), "sift", cv2.NORM_L2
        except cv2.error:  # pragma: no cover - build-specific
            pass
    return (
        cv2.ORB_create(nfeatures=REGISTRATION.max_features, fastThreshold=7),
        "orb",
        cv2.NORM_HAMMING,
    )


# --------------------------------------------------------------------------
# Homography validation
# --------------------------------------------------------------------------


def _decompose(matrix: np.ndarray) -> tuple[float, float, float, float, float]:
    """Return (scale, rotation_deg, shear, perspective, translation_px)."""
    a, b = float(matrix[0, 0]), float(matrix[0, 1])
    c, d = float(matrix[1, 0]), float(matrix[1, 1])
    tx, ty = float(matrix[0, 2]), float(matrix[1, 2])
    p = math.hypot(float(matrix[2, 0]), float(matrix[2, 1]))

    scale_x = math.hypot(a, c)
    scale_y = math.hypot(b, d)
    scale = math.sqrt(max(scale_x * scale_y, 1e-12))
    rotation = math.degrees(math.atan2(c, a))

    # Shear = departure from orthogonality of the two basis vectors.
    denom = scale_x * scale_y
    shear = abs(a * b + c * d) / denom if denom > 1e-12 else 1.0

    return scale, rotation, shear, p, math.hypot(tx, ty)


def _is_plausible(matrix: np.ndarray) -> tuple[bool, str]:
    cfg = REGISTRATION
    if not np.all(np.isfinite(matrix)):
        return False, "transform contained non-finite values"

    scale, rotation, shear, perspective, _ = _decompose(matrix)

    if not (cfg.min_scale_change <= scale <= cfg.max_scale_change):
        return False, f"implausible scale change ({scale:.2f}x)"
    if abs(rotation) > cfg.max_rotation_deg:
        return False, f"implausible rotation ({rotation:.1f} deg)"
    if shear > cfg.max_shear:
        return False, f"implausible shear ({shear:.2f})"
    if perspective > cfg.max_perspective:
        return False, "implausible perspective component"
    return True, ""


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------


def _feature_align(
    ref_gray: np.ndarray, test_gray: np.ndarray
) -> tuple[np.ndarray | None, str, int, int]:
    detector, name, norm = _detector()

    kp_ref, des_ref = detector.detectAndCompute(ref_gray, None)
    kp_test, des_test = detector.detectAndCompute(test_gray, None)

    if des_ref is None or des_test is None or len(kp_ref) < 8 or len(kp_test) < 8:
        return None, name, 0, 0

    matcher = cv2.BFMatcher(norm)
    try:
        knn = matcher.knnMatch(des_test, des_ref, k=2)
    except cv2.error:  # pragma: no cover - degenerate descriptors
        return None, name, 0, 0

    # Lowe's ratio test: keep a match only when the best candidate is clearly
    # better than the runner-up. Solder mask and silkscreen are full of
    # near-duplicate corners, so this filter is doing most of the work.
    good = []
    for pair in knn:
        if len(pair) != 2:
            continue
        best, second = pair
        if best.distance < REGISTRATION.ratio_test * second.distance:
            good.append(best)

    if len(good) < REGISTRATION.min_good_matches:
        return None, name, 0, len(good)

    src = np.float32([kp_test[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp_ref[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    matrix, mask = cv2.findHomography(
        src, dst, cv2.RANSAC, REGISTRATION.ransac_reproj_threshold, maxIters=5000
    )
    if matrix is None or mask is None:
        return None, name, 0, len(good)

    inliers = int(mask.sum())
    return matrix.astype(np.float64), name, inliers, len(good)


def _ecc_align(ref_gray: np.ndarray, test_gray: np.ndarray) -> np.ndarray | None:
    """Intensity-based affine alignment on a downscaled pair."""
    long_edge = max(ref_gray.shape[:2])
    scale = min(1.0, REGISTRATION.ecc_downscale_long_edge / max(long_edge, 1))

    if scale < 1.0:
        size = (int(ref_gray.shape[1] * scale), int(ref_gray.shape[0] * scale))
        small_ref = cv2.resize(ref_gray, size, interpolation=cv2.INTER_AREA)
        small_test = cv2.resize(test_gray, size, interpolation=cv2.INTER_AREA)
    else:
        small_ref, small_test = ref_gray, test_gray

    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (
        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
        REGISTRATION.ecc_iterations,
        REGISTRATION.ecc_epsilon,
    )

    try:
        cv2.findTransformECC(
            small_ref.astype(np.float32) / 255.0,
            small_test.astype(np.float32) / 255.0,
            warp,
            cv2.MOTION_AFFINE,
            criteria,
            None,
            5,
        )
    except cv2.error:
        return None

    matrix = np.eye(3, dtype=np.float64)
    matrix[:2, :] = warp.astype(np.float64)

    if scale < 1.0:
        # Un-scale: T_full = S^-1 . T_small . S
        s = np.diag([scale, scale, 1.0]).astype(np.float64)
        s_inv = np.diag([1.0 / scale, 1.0 / scale, 1.0]).astype(np.float64)
        matrix = s_inv @ matrix @ s

    return matrix


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def warp_to_reference(
    test_bgr: np.ndarray, matrix: np.ndarray, reference_shape: tuple[int, int]
) -> np.ndarray:
    h, w = reference_shape[:2]
    return cv2.warpPerspective(
        test_bgr,
        matrix,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def coverage_mask(
    test_shape: tuple[int, ...],
    matrix: np.ndarray,
    reference_shape: tuple[int, ...],
    content_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Which reference pixels the test photograph actually shows.

    A photo of half the board still aligns perfectly well -- the features it
    does contain are real. What changes is that half the reference has no
    evidence behind it at all. Warping a solid white rectangle through the
    same transform says exactly where that boundary falls, so the parts
    outside it can be reported as unseen instead of guessed at.
    """
    solid = (
        content_mask if content_mask is not None
        else np.full(test_shape[:2], 255, dtype=np.uint8)
    )
    height, width = reference_shape[:2]
    return cv2.warpPerspective(
        solid, matrix, (width, height),
        flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )


def _board_correlation(
    ref_gray: np.ndarray, warped_gray: np.ndarray, mask: np.ndarray | None = None
) -> float:
    """Zero-mean normalised correlation, restricted to the covered area.

    Measuring over the whole frame would punish a partial photo for the region
    it never claimed to show, and report a good alignment as a bad one.
    """
    if mask is not None and np.count_nonzero(mask) > 256:
        a = ref_gray[mask > 0].astype(np.float32)
        b = warped_gray[mask > 0].astype(np.float32)
    else:
        a = ref_gray.astype(np.float32).ravel()
        b = warped_gray.astype(np.float32).ravel()
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-6:
        return 0.0
    return float(np.clip(float(np.dot(a, b)) / denom, -1.0, 1.0))


def align(reference_bgr: np.ndarray, test_bgr: np.ndarray) -> Alignment:
    """Align ``test_bgr`` onto ``reference_bgr``.

    Always returns an :class:`Alignment` -- never raises. A failed alignment is
    reported as identity with warnings so the operator sees *why* a result is
    untrustworthy instead of getting a silent wall of false positives.
    """
    ref_gray = _prepare(reference_bgr)
    test_gray = _prepare(test_bgr)

    warnings: list[str] = []

    matrix, method, inliers, total = _feature_align(ref_gray, test_gray)
    inlier_ratio = (inliers / total) if total else 0.0

    if matrix is not None:
        ok, reason = _is_plausible(matrix)
        if not ok:
            warnings.append(f"Feature alignment rejected: {reason}.")
            matrix = None
        elif inlier_ratio < REGISTRATION.min_inlier_ratio:
            warnings.append(
                f"Feature alignment rejected: only {inliers}/{total} consistent matches."
            )
            matrix = None

    if matrix is None:
        ecc = _ecc_align(ref_gray, test_gray)
        if ecc is not None:
            ok, reason = _is_plausible(ecc)
            if ok:
                matrix, method = ecc, "ecc"
                inliers = total = 0
                inlier_ratio = 0.0
            else:
                warnings.append(f"Intensity alignment rejected: {reason}.")

    if matrix is None:
        matrix = np.eye(3, dtype=np.float64)
        method = "identity"
        warnings.append(
            "Could not align this photo to the reference. "
            "Results assume the camera did not move."
        )

    warped_gray = cv2.warpPerspective(
        test_gray, matrix, (ref_gray.shape[1], ref_gray.shape[0]),
        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE,
    )
    visible = coverage_mask(test_gray.shape, matrix, ref_gray.shape)
    coverage = float(np.count_nonzero(visible)) / float(visible.size)
    correlation = _board_correlation(ref_gray, warped_gray, visible)

    scale, rotation, _, _, translation = _decompose(matrix)

    # Quality is dominated by how well the boards actually overlap; the inlier
    # ratio only nudges it. A beautiful homography that lands on the wrong
    # board should not read as high quality.
    quality = float(np.clip(0.80 * max(correlation, 0.0) + 0.20 * inlier_ratio, 0.0, 1.0))

    if correlation < REGISTRATION.low_quality_warning:
        warnings.append(
            "Low overlap with the reference after alignment. "
            "Check that this is the same board photographed the same way."
        )

    return Alignment(
        matrix=matrix,
        method=method,
        inliers=inliers,
        total_matches=total,
        inlier_ratio=inlier_ratio,
        translation_px=translation,
        rotation_deg=rotation,
        scale=scale,
        board_correlation=correlation,
        quality=quality,
        coverage=coverage,
        warnings=warnings,
    )
