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

#: Refused before decoding. The byte limit above does not bound memory: a
#: compressed image is free to describe far more pixels than it occupies on
#: disk, and those pixels are allocated before any of this code runs. Sixty
#: megapixels is already several times more than the working size needs.
MAX_DECODED_PIXELS = 60_000_000
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

    #: Measurements are taken on the ROI shrunk by this much on every side.
    #:
    #: An operator's box encloses the component *and* a ring of its footprint:
    #: solder pads, silkscreen outline, the via fanout. That ring is identical
    #: whether the part is fitted or not, so including it dilutes every signal
    #: with pixels that cannot carry the answer. On a small IC the pads were
    #: holding correlation at 0.74 and edge agreement at 0.80 for a part that
    #: had been removed outright.
    #:
    #: Measuring the inner region instead concentrates on the body. The ring is
    #: still used for the local re-alignment search, where its contrast is an
    #: asset rather than a liability.
    body_inset_fraction: float = 0.14
    max_body_inset_px: int = 7
    min_body_px: int = 10

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

#: Judge each photograph against its own noise floor as well as the fixed
#: threshold. Under a single nuisance the engine is comfortably inside its
#: margins, but a hand-held photo brings several at once and the scores of
#: *present* parts creep upward together. Measured on such photos, the
#: ordering stays right, a missing part still scores well above every present
#: one on 38 of 42 boards, but the fixed line sits too low and a handful of
#: present parts cross it. So the line is also placed this many robust
#: deviations above the middle of the scores on this photo, and the stricter
#: of the two is used. It can only ever raise the bar, never lower it, so it
#: cannot turn a part the fixed threshold would have caught into a pass.
#: Zero switches it off.
ADAPTIVE_THRESHOLD_K = 3.0

#: Never let the adaptive line rise above this, however strange the photo.
ADAPTIVE_THRESHOLD_CAP = 0.80


@dataclass(frozen=True)
class SensitivityPreset:
    key: str
    label: str
    description: str
    missing_threshold: float
    review_threshold: float


#: Read off the operating curve in ``tools/evaluate.py --sweep``, not chosen
#: by eye. Over 3,280 part decisions on the 80-board stress set the
#: highest-scoring part that is actually present scores 0.373, and the
#: weakest genuine removal scores 0.652. Every threshold in that gap gives
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
        missing_threshold=0.58,
        review_threshold=0.49,
    ),
    "balanced": SensitivityPreset(
        key="balanced",
        label="Balanced",
        description="Recommended for a fixed camera on a stable line.",
        missing_threshold=0.52,
        review_threshold=0.44,
    ),
    "sensitive": SensitivityPreset(
        key="sensitive",
        label="Sensitive",
        description="Catches subtle removals. Expect more review flags.",
        missing_threshold=0.44,
        review_threshold=0.40,
    ),
}

DEFAULT_SENSITIVITY = "balanced"

# --------------------------------------------------------------------------
# Component suggestion (assists the operator while naming parts)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SuggestConfig:
    #: Upper bound on proposed boxes, as a guard against a pathological image
    #: producing thousands, not as an opinion about how many parts a board
    #: has. It was 40, which silently truncated a 41-component board and
    #: dropped the smallest part: the detector had found it, the cap threw it
    #: away, and the result looked like a detection failure.
    #:
    #: Real assemblies run to hundreds of parts, so the guard sits far above
    #: anything a person would annotate by hand.
    max_suggestions: int = 400
    min_area_fraction: float = 0.00035   # of board area
    max_area_fraction: float = 0.09
    max_aspect_ratio: float = 9.0
    pad_px: int = 3
    merge_iou: float = 0.40


SUGGEST = SuggestConfig()

# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

#: A highlight is called a highlight when the light on the test photo is this
#: much stronger than on the reference, measured after both are blurred down
#: to the illumination alone.
GLARE_RATIO = 1.30

# A per-part version of this was tried: if a highlight covered most of a
# part, its missing verdict was downgraded to review, on the reasoning that a
# reflection hides a fitted part and an empty footprint equally well. It was
# removed after measurement. Ablating the nuisances one at a time showed that
# reflections were never what drove the false alarms, and the rule suppressed
# eleven real removals, because lifting a dark chip off the board leaves
# something brighter behind and that is what a highlight looks like. The
# highlight mask is now measured on bare board only and used for the exposure
# match and the warning, never to silence a part.

#: Fraction of the whole board under a highlight before the operator is told
#: to retake the photograph.
GLARE_BOARD_FRACTION = 0.02

STATE_PRESENT = "present"
STATE_MISSING = "missing"
STATE_REVIEW = "review"
STATE_NOT_VISIBLE = "not_visible"

#: How much of a part's box the photograph must actually show before the
#: engine is willing to judge it.
#:
#: A photo of half the board aligns perfectly well -- the features it contains
#: are real -- but the other half has no evidence behind it. Calling those
#: parts "missing" would be a confident lie, and calling them "present" an
#: equally confident one. They are reported as unseen instead, and the summary
#: says how many of the named parts were actually checked.
MIN_PART_COVERAGE = 0.70

APP_NAME = "PCB Missing-Component Inspector"
APP_VERSION = "1.0.0"


# --------------------------------------------------------------------------
# Reading the printed designators (a typing shortcut, never a verdict)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SilkscreenConfig:
    """Tunables for app.silkscreen.

    Nothing here can change a missing/present decision. The worst a bad value
    can do is leave more names for the operator to type. Every length is a
    multiple of the board's own printed text height, measured from the board
    during the survey pass, so these hold at any image size.
    """

    #: Enlargement for the one survey pass over the whole board. Three times
    #: is enough to locate labels; the survey only has to find a minority of
    #: them, and reading the full board larger costs seconds for no gain.
    survey_upscale: float = 3.0

    #: Below this many designator-shaped strings the board is not one this
    #: reader understands, so it declines rather than inventing a convention
    #: from two or three readings.
    min_seeds: int = 6

    #: How far from a part, in text heights, a label may sit and still be
    #: considered a candidate for it. Generous on purpose: wrong pairings are
    #: expected and disagree with each other, so they cost accuracy nothing.
    pairing_reach: float = 9.0

    #: How close two offsets must be, in text heights, to count as the same
    #: convention. Roughly one character.
    offset_tolerance: float = 1.2

    #: Fewer agreeing pairs than this is not a convention, it is a
    #: coincidence, so the reader declines and every name is typed.
    min_votes: int = 6

    #: Half-size of the strip read for one label, in text heights. Wide enough
    #: for a four-character designator, tight enough to exclude the next one.
    #: Measured: widening it to 4.6 lost two labels to neighbouring text.
    strip_half_width: float = 3.4
    strip_half_height: float = 1.4

    #: Target glyph height in pixels when the strip is enlarged for reading.
    #: Swept over two boards: 80 read 61 of 82 labels, 100 read 64, and going
    #: wider as well as larger started producing wrong answers.
    strip_text_height: float = 100.0

    #: How far from the predicted spot, in text heights, a reading may sit and
    #: still be taken as this part's label. Measured over 122 correct readings
    #: on two boards, the furthest a real label sat was 0.23, so this is about
    #: six times the observed spread: generous enough that no genuine reading
    #: is refused, tight enough that a neighbour's label caught at the edge of
    #: the strip is. Without it there was no limit at all.
    max_label_offset: float = 1.5

    #: Used only if the survey found text but could not measure its height.
    fallback_text_height: float = 10.0
