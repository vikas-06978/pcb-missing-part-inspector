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

    #: CIELAB chroma histogram resolution, and the Gaussian smoothing applied
    #: to it before comparison so that bin quantisation is not mistaken for a
    #: colour change.
    chroma_bins: int = 24
    chroma_smoothing: float = 1.2


ANALYSIS = AnalysisConfig()


@dataclass(frozen=True)
class SignalRamps:
    """Breakpoints converting each raw measurement into a 0..1 "missingness".

    Each tuple is ``(low, high)``. For similarity-style signals the score is
    ``1 - ramp(value, low, high)``; for distance-style signals it is
    ``ramp(value, low, high)``.
    """

    ncc: tuple[float, float] = (0.30, 0.80)               # similarity
    ssim: tuple[float, float] = (0.25, 0.75)              # similarity
    edge_correlation: tuple[float, float] = (0.25, 0.80)  # similarity
    #: Widened from (0.12, 0.42), which saturated at 1.0 for a present part in
    #: a harsh photo and for a genuinely removed IC alike -- destroying the
    #: distinction this channel exists to make. A removed part reaches ~0.85;
    #: white-balance drift on a part that is still there reaches ~0.5.
    colour_distance: tuple[float, float] = (0.25, 0.85)   # distance
    #: Widened from (0.15, 0.90): ordinary defocus costs ~30% of gradient
    #: energy on a part that is plainly still there.
    gradient_log_ratio: tuple[float, float] = (0.25, 1.10)  # distance
    residual: tuple[float, float] = (0.50, 2.20)          # distance


RAMPS = SignalRamps()


@dataclass(frozen=True)
class FusionWeights:
    """Weights of the six evidence channels. Must sum to 1.0.

    Weighted by how well each channel separates *a removal* from *a bad
    photograph of a complete board*, measured on the 80-board stress set --
    not by how large a number each produces.

    Correlation (NCC), outline agreement and chroma all survive exposure,
    white balance and focus changes by construction, and move sharply when a
    part actually goes. Gradient amplitude and raw pixel residual do not: on
    the worst photographs they scored *higher* for present parts than for
    genuinely missing ones, because they were largely measuring the nuisance.
    They are kept at low weight as tie-breakers rather than dropped, since
    they are the channels that catch a part replaced by something of the same
    colour and outline.
    """

    ncc: float = 0.28
    ssim: float = 0.14
    edge_correlation: float = 0.24
    colour_distance: float = 0.26
    gradient_log_ratio: float = 0.04
    residual: float = 0.04

    def as_dict(self) -> dict[str, float]:
        return {
            "ncc": self.ncc,
            "ssim": self.ssim,
            "edge_correlation": self.edge_correlation,
            "colour_distance": self.colour_distance,
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

    #: A correlation peak this high means the component's own pattern was
    #: found, at this location, at this scale. Nothing else on a board
    #: produces that. It alone settles the question.
    decisive_ncc: float = 0.92
    decisive_cap: float = 0.15

    strong_ncc: float = 0.84
    strong_edge_correlation: float = 0.75
    strong_cap: float = 0.18

    good_ncc: float = 0.72
    good_edge_correlation: float = 0.60
    good_colour: float = 0.22
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


#: Read off the operating curve in ``tools/evaluate.py --sweep``, not chosen
#: by eye. Over 1,920 part decisions on the 80-board stress set the
#: highest-scoring part that is actually present scores 0.247, and the
#: weakest genuine removal scores 0.424. Every threshold in that gap gives
#: 100% precision and 100% recall, so the presets sit inside it with margin
#: on both sides rather than balanced on an edge.
#:
#: These numbers come from a synthetic benchmark and should be read as
#: evidence that the decision is not knife-edge, not as a promise of
#: real-world accuracy. The presets exist precisely so that a line with
#: different optics can be moved without recompiling anything.
SENSITIVITY_PRESETS: dict[str, SensitivityPreset] = {
    "conservative": SensitivityPreset(
        key="conservative",
        label="Conservative",
        description="Flags only unmistakable removals. Fewest false alarms.",
        missing_threshold=0.40,
        review_threshold=0.32,
    ),
    "balanced": SensitivityPreset(
        key="balanced",
        label="Balanced",
        description="Recommended for a fixed camera on a stable line.",
        missing_threshold=0.34,
        review_threshold=0.27,
    ),
    "sensitive": SensitivityPreset(
        key="sensitive",
        label="Sensitive",
        description="Catches subtle removals. Expect more review flags.",
        missing_threshold=0.28,
        review_threshold=0.23,
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
    merge_iou: float = 0.40


SUGGEST = SuggestConfig()

# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

STATE_PRESENT = "present"
STATE_MISSING = "missing"
STATE_REVIEW = "review"

APP_NAME = "PCB Missing-Component Inspector"
APP_VERSION = "1.0.0"
