"""Central configuration and tuned constants for the inspection engine.

Every number that influences a PRESENT / MISSING decision lives here so that it
can be reviewed, version-controlled and re-tuned against the evaluation set in
``tools/evaluate.py`` without touching algorithm code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
DATA_DIR = ROOT_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"

for _d in (DATA_DIR, PROJECTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Image handling
# --------------------------------------------------------------------------

#: Long edge the reference frame is normalised to. Large enough that a 0402
#: passive is still a few dozen pixels wide, small enough to stay interactive.
WORKING_LONG_EDGE = 1800

#: Images smaller than this on the long edge are rejected as unusable.
MIN_LONG_EDGE = 320

ALLOWED_UPLOAD_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/bmp",
    "image/webp",
    "image/tiff",
}

MAX_UPLOAD_BYTES = 40 * 1024 * 1024  # 40 MB per image
MAX_TEST_IMAGES_PER_RUN = 12

# --------------------------------------------------------------------------
# Registration (test frame -> reference frame)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistrationConfig:
    max_features: int = 4000
    ratio_test: float = 0.78
    min_good_matches: int = 12
    ransac_reproj_threshold: float = 3.0
    min_inlier_ratio: float = 0.30

    # Sanity envelope for the recovered homography. A fixed factory camera can
    # drift a little; anything outside this is a different photo, not a nudge.
    max_scale_change: float = 1.35
    min_scale_change: float = 0.74
    max_rotation_deg: float = 20.0
    max_shear: float = 0.25
    max_perspective: float = 2.5e-3

    # ECC fallback (used when feature matching fails or is rejected).
    ecc_iterations: int = 220
    ecc_epsilon: float = 1e-6
    ecc_downscale_long_edge: int = 900

    #: Below this whole-board correlation we warn the user that the two photos
    #: may not be the same board / viewpoint.
    low_quality_warning: float = 0.45


REGISTRATION = RegistrationConfig()

# --------------------------------------------------------------------------
# Per-part analysis
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisConfig:
    #: Search margin around each ROI, as a fraction of the ROI's short side,
    #: clamped to [min_margin_px, max_margin_px]. Absorbs residual registration
    #: error so that a 3 px drift is not read as a missing part.
    margin_fraction: float = 0.22
    min_margin_px: int = 6
    max_margin_px: int = 28

    #: ROIs smaller than this are upscaled before measurement so that the
    #: statistics (SSIM windows, Canny) remain meaningful.
    min_patch_px: int = 24

    #: Below this correlation peak the recovered offset is meaningless, so the
    #: remaining metrics are measured at zero offset instead.
    offset_trust_ncc: float = 0.20

    edge_dilate_px: int = 2
    hist_bins_h: int = 32
    hist_bins_s: int = 32


ANALYSIS = AnalysisConfig()


@dataclass(frozen=True)
class SignalRamps:
    """Breakpoints converting each raw measurement into a 0..1 "missingness".

    Each tuple is ``(low, high)``. For similarity-style signals the score is
    ``1 - ramp(value, low, high)``; for distance-style signals it is
    ``ramp(value, low, high)``.
    """

    ncc: tuple[float, float] = (0.30, 0.80)          # similarity
    ssim: tuple[float, float] = (0.25, 0.75)         # similarity
    edge_iou: tuple[float, float] = (0.15, 0.55)     # similarity
    hist_distance: tuple[float, float] = (0.20, 0.60)  # distance
    gradient_log_ratio: tuple[float, float] = (0.15, 0.90)  # distance
    residual: tuple[float, float] = (0.35, 1.20)     # distance


RAMPS = SignalRamps()


@dataclass(frozen=True)
class FusionWeights:
    """Weights of the six evidence channels. Must sum to 1.0."""

    ncc: float = 0.26
    ssim: float = 0.22
    edge_iou: float = 0.18
    hist_distance: float = 0.14
    gradient_log_ratio: float = 0.10
    residual: float = 0.10

    def as_dict(self) -> dict[str, float]:
        return {
            "ncc": self.ncc,
            "ssim": self.ssim,
            "edge_iou": self.edge_iou,
            "hist_distance": self.hist_distance,
            "gradient_log_ratio": self.gradient_log_ratio,
            "residual": self.residual,
        }


WEIGHTS = FusionWeights()


@dataclass(frozen=True)
class VetoConfig:
    """Hard "this part is clearly still there" rules.

    False alarms are far more expensive than a missed call on a factory line:
    an operator who is paged for nothing three times stops trusting the tool.
    These vetoes cap the confidence when the evidence for *presence* is
    overwhelming, regardless of what the weighted sum says.
    """

    strong_ncc: float = 0.86
    strong_ssim: float = 0.78
    strong_cap: float = 0.18

    good_ncc: float = 0.75
    good_edge_iou: float = 0.55
    good_hist: float = 0.25
    good_cap: float = 0.28


VETO = VetoConfig()


@dataclass(frozen=True)
class SceneCompensation:
    """Population-level correction for a globally degraded photo.

    If *every* ROI looks slightly wrong, the cause is usually the photograph
    (exposure, focus, white balance), not forty simultaneously missing parts.
    We subtract part of the population median, but cap how much we are willing
    to explain away so that a genuinely stripped board still alarms.
    """

    enabled: bool = True
    min_parts: int = 5
    trigger_median: float = 0.30
    strength: float = 0.60
    max_subtraction: float = 0.15


SCENE = SceneCompensation()


@dataclass(frozen=True)
class SensitivityPreset:
    key: str
    label: str
    description: str
    missing_threshold: float
    review_threshold: float


SENSITIVITY_PRESETS: dict[str, SensitivityPreset] = {
    "conservative": SensitivityPreset(
        key="conservative",
        label="Conservative",
        description="Flags only unmistakable removals. Fewest false alarms.",
        missing_threshold=0.62,
        review_threshold=0.46,
    ),
    "balanced": SensitivityPreset(
        key="balanced",
        label="Balanced",
        description="Recommended for a fixed camera on a stable line.",
        missing_threshold=0.52,
        review_threshold=0.38,
    ),
    "sensitive": SensitivityPreset(
        key="sensitive",
        label="Sensitive",
        description="Catches subtle removals. Expect more review flags.",
        missing_threshold=0.42,
        review_threshold=0.30,
    ),
}

DEFAULT_SENSITIVITY = "balanced"

# --------------------------------------------------------------------------
# Component suggestion (assists the operator while naming parts)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SuggestConfig:
    max_suggestions: int = 40
    min_area_fraction: float = 0.00035   # of board area
    max_area_fraction: float = 0.09
    max_aspect_ratio: float = 9.0
    pad_px: int = 3
    merge_iou: float = 0.30


SUGGEST = SuggestConfig()

# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

STATE_PRESENT = "present"
STATE_MISSING = "missing"
STATE_REVIEW = "review"

APP_NAME = "PCB Missing-Component Inspector"
APP_VERSION = "1.0.0"
