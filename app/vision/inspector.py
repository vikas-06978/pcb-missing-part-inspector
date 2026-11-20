"""The inspection engine: decide, per named part, whether it is still there.

Pipeline for one test photograph
--------------------------------
1. ``registration`` warps the test frame into the reference frame.
2. ``photometric`` equalises exposure so absolute thresholds mean something.
3. For every annotated ROI we re-align locally (a few pixels of residual
   drift) and measure six independent, deliberately redundant signals.
4. The signals are mapped to a 0..1 "missingness" each and fused with fixed
   weights, then passed through presence vetoes.
5. A population-level correction absorbs a globally bad photograph.
6. Thresholds turn the confidence into PRESENT / REVIEW / MISSING.

Why six signals and not one
---------------------------
Any single metric has a failure mode that a factory will find within a shift.
SSIM alone is fooled by a change of focus. Colour alone is fooled by a white
sticker. Edges alone are fooled by exposed pads that happen to be as busy as
the part that covered them. Agreement across independent measurements is what
makes the verdict survive unseen boards -- which is exactly what the live demo
is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

import cv2
import numpy as np

from app.config import (
    ANALYSIS,
    DEFAULT_SENSITIVITY,
    MIN_PART_COVERAGE,
    RAMPS,
    SCENE,
    SENSITIVITY_PRESETS,
    STATE_MISSING,
    STATE_NOT_VISIBLE,
    STATE_PRESENT,
    GLARE_BOARD_FRACTION,
    GLARE_RATIO,
    ADAPTIVE_THRESHOLD_CAP,
    ADAPTIVE_THRESHOLD_K,
    STATE_REVIEW,
    VETO,
    WEIGHTS,
)
from app.vision import metrics as M
from app.vision import photometric as P
from app.vision.photometric import board_mask, match_levels, match_sharpness
from app.vision.registration import Alignment, align, coverage_mask, warp_to_reference

# Human-readable explanation for each evidence channel when it fires.
SIGNAL_LABELS: dict[str, tuple[str, str]] = {
    "ncc": ("Template match", "the part's pattern is no longer found here"),
    "ssim": ("Structure", "the local structure no longer matches the reference"),
    "edge_correlation": ("Outline", "the component outline is absent"),
    "colour_distance": ("Colour", "the colour now reads as bare board, not the part"),
    "gradient_log_ratio": ("Texture", "surface texture and markings are gone"),
    "residual": ("Pixel difference", "the pixels differ strongly from the reference"),
}


@dataclass
class PartVerdict:
    part_id: str
    name: str
    bbox: list[int]
    state: str
    confidence: float
    raw_confidence: float
    coverage: float
    offset: list[int]
    signals: dict[str, float]
    scores: dict[str, float]
    evidence: list[dict[str, Any]]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InspectionResult:
    verdicts: list[PartVerdict]
    alignment: Alignment
    photometric: dict[str, float]
    sharpness: dict[str, float]
    scene_compensation: float
    sensitivity: str
    thresholds: dict[str, float]
    warnings: list[str] = field(default_factory=list)
    aligned_image: np.ndarray | None = None

    # ---- convenience -----------------------------------------------------

    @property
    def missing(self) -> list[PartVerdict]:
        return [v for v in self.verdicts if v.state == STATE_MISSING]

    @property
    def review(self) -> list[PartVerdict]:
        return [v for v in self.verdicts if v.state == STATE_REVIEW]

    @property
    def not_visible(self) -> list[PartVerdict]:
        return [v for v in self.verdicts if v.state == STATE_NOT_VISIBLE]

    @property
    def is_ok(self) -> bool:
        return not self.missing and not self.review

    def summary(self) -> dict[str, Any]:
        missing = self.missing
        review = self.review
        unseen = self.not_visible
        checked = len(self.verdicts) - len(unseen)
        if missing:
            status = STATE_MISSING
            # Say both numbers when there are both. A board that reads
            # "5 parts missing" while another five sit waiting for someone to
            # look at them has told the operator the smaller half of the
            # story, and the half it left out is the half that needs them.
            headline = (
                f"{len(missing)} part missing" if len(missing) == 1
                else f"{len(missing)} parts missing"
            )
            if review:
                headline += (
                    ", 1 to check" if len(review) == 1
                    else f", {len(review)} to check"
                )
        elif review:
            status = STATE_REVIEW
            headline = (
                f"{len(review)} part needs review" if len(review) == 1
                else f"{len(review)} parts need review"
            )
        elif checked == 0:
            status = STATE_NOT_VISIBLE
            headline = "No named parts in view"
        else:
            status = STATE_PRESENT
            # Never a bare "OK" for a partial photo: it would read as a verdict
            # on the whole board when half of it was never shown.
            headline = "OK" if not unseen else f"OK for {checked} of {len(self.verdicts)} parts"

        return {
            "status": status,
            "headline": headline,
            "total_parts": len(self.verdicts),
            "checked_count": checked,
            "missing_count": len(missing),
            "review_count": len(review),
            "not_visible_count": len(unseen),
            "present_count": checked - len(missing) - len(review),
            "missing_names": [v.name for v in missing],
            "review_names": [v.name for v in review],
            "not_visible_names": [v.name for v in unseen],
        }


# --------------------------------------------------------------------------
# Patch helpers
# --------------------------------------------------------------------------


def _clamp_bbox(bbox: Sequence[int], width: int, height: int) -> tuple[int, int, int, int]:
    x, y, w, h = (int(round(float(v))) for v in bbox)
    x = max(0, min(x, max(width - 1, 0)))
    y = max(0, min(y, max(height - 1, 0)))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


def _margin_for(w: int, h: int) -> int:
    raw = int(round(ANALYSIS.margin_fraction * min(w, h)))
    return int(np.clip(raw, ANALYSIS.min_margin_px, ANALYSIS.max_margin_px))


def _patch(image: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray | None:
    height, width = image.shape[:2]
    if x < 0 or y < 0 or x + w > width or y + h > height or w <= 0 or h <= 0:
        return None
    return image[y:y + h, x:x + w]


# --------------------------------------------------------------------------
# Per-part analysis
# --------------------------------------------------------------------------


def _surround_pixels(
    image: np.ndarray, x: int, y: int, w: int, h: int, margin: int
) -> np.ndarray:
    """The board just outside a component's box, as a flat list of pixels.

    Used to measure the light on this part without looking at the part. The
    box itself is cut out, so what comes back is bare board, pads and
    silkscreen, all of which are there whether or not the component is.
    """
    height, width = image.shape[:2]
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(width, x + w + margin), min(height, y + h + margin)
    window = image[y0:y1, x0:x1]
    if window.size == 0:
        return np.empty((0, 3), image.dtype)

    keep = np.ones(window.shape[:2], bool)
    keep[max(0, y - y0):max(0, y - y0) + h, max(0, x - x0):max(0, x - x0) + w] = False
    return window[keep]


def _analyse_part(
    reference_bgr: np.ndarray,
    aligned_bgr: np.ndarray,
    bbox: Sequence[int],
) -> tuple[dict[str, float], dict[str, float], tuple[int, int]]:
    """Measure one ROI. Returns (raw signals, missingness sub-scores, offset)."""
    height, width = reference_bgr.shape[:2]
    x, y, w, h = _clamp_bbox(bbox, width, height)
    margin = _margin_for(w, h)

    ref_patch = reference_bgr[y:y + h, x:x + w]

    # --- local re-alignment -------------------------------------------------
    ex0, ey0 = max(0, x - margin), max(0, y - margin)
    ex1, ey1 = min(width, x + w + margin), min(height, y + h + margin)
    search_region = aligned_bgr[ey0:ey1, ex0:ex1]

    ncc, dx, dy = M.best_offset_ncc(
        ref_patch, search_region, anchor=(x - ex0, y - ey0)
    )

    # A meaningless peak gives a meaningless offset; fall back to zero drift.
    if ncc < ANALYSIS.offset_trust_ncc:
        dx = dy = 0

    test_patch = _patch(aligned_bgr, x + dx, y + dy, w, h)
    if test_patch is None:
        dx = dy = 0
        test_patch = _patch(aligned_bgr, x, y, w, h)
    if test_patch is None:  # pragma: no cover - bbox already clamped
        test_patch = np.zeros_like(ref_patch)

    # --- narrow to the component body ---------------------------------------
    # The search above wanted the footprint ring for its contrast; the
    # measurements below want it gone, because pads and silkscreen look the
    # same with or without the part sitting on them.
    inset = int(np.clip(
        round(ANALYSIS.body_inset_fraction * min(w, h)), 0, ANALYSIS.max_body_inset_px
    ))
    body_w, body_h = w - 2 * inset, h - 2 * inset

    ref_body = test_body = None
    if inset > 0 and body_w >= ANALYSIS.min_body_px and body_h >= ANALYSIS.min_body_px:
        ref_body = _patch(reference_bgr, x + inset, y + inset, body_w, body_h)
        test_body = _patch(aligned_bgr, x + dx + inset, y + dy + inset, body_w, body_h)

    if ref_body is None or test_body is None:
        ref_body, test_body = ref_patch, test_patch

    # --- correct for the light falling on this part -------------------------
    # Sample the board surrounding the component, in both photographs, and
    # scale the test patch by the ratio. A shadow or a highlight lying across
    # part of the board is local, so the global exposure match cannot see it;
    # this can, because it only ever compares bare board against bare board.
    # The component's own pixels are excluded from the estimate, so a part
    # that really is missing cannot talk its way into looking present.
    ring_ref = _surround_pixels(reference_bgr, x, y, w, h, margin)
    ring_test = _surround_pixels(aligned_bgr, x + dx, y + dy, w, h, margin)
    test_body, local_gain = P.match_levels_around(ring_ref, ring_test, test_body)

    # --- measurements -------------------------------------------------------
    ref_small = M.ensure_min_size(ref_body)
    test_small = M.ensure_min_size(test_body)

    ref_gray = M.to_gray(ref_small)
    test_gray = M.to_gray(test_small)

    signals = {
        # Correlation of the body alone. The full-ROI search peak is kept
        # separately below: it answers "where is it", not "is it there".
        "ncc": M.patch_correlation(ref_small, test_small),
        "match_peak": float(ncc),
        "ssim": M.ssim(ref_gray, test_gray),
        "edge_correlation": M.edge_correlation(ref_gray, test_gray),
        "colour_distance": M.colour_distance(ref_small, test_small),
        "gradient_log_ratio": M.gradient_log_ratio(ref_gray, test_gray),
        "residual": M.normalised_residual(ref_gray, test_gray),
        "intensity_shift": M.mean_intensity_shift(ref_gray, test_gray),
        "ref_edge_density": M.edge_density(ref_gray),
        "test_edge_density": M.edge_density(test_gray),
        "local_gain": float(local_gain),
        # How much of this part the light has burned away. Saturated pixels
        # carry no information about what is underneath them, so a part lying
        # under a reflection cannot be judged present or absent at all.
        "blown_out": M.saturated_fraction(test_small),
        "ref_blown_out": M.saturated_fraction(ref_small),
        "ref_contrast": float(ref_gray.std()),
        "test_contrast": float(test_gray.std()),
    }

    scores = {
        "ncc": M.inv_ramp(signals["ncc"], *RAMPS.ncc),
        "ssim": M.inv_ramp(signals["ssim"], *RAMPS.ssim),
        "edge_correlation": M.inv_ramp(
            signals["edge_correlation"], *RAMPS.edge_correlation
        ),
        "colour_distance": M.ramp(signals["colour_distance"], *RAMPS.colour_distance),
        "gradient_log_ratio": M.ramp(
            signals["gradient_log_ratio"], *RAMPS.gradient_log_ratio
        ),
        "residual": M.ramp(signals["residual"], *RAMPS.residual),
    }

    return signals, scores, (dx, dy)


def _fuse(signals: dict[str, float], scores: dict[str, float]) -> float:
    """Weighted fusion plus presence vetoes. Returns raw confidence 0..1."""
    weights = WEIGHTS.as_dict()
    confidence = sum(weights[k] * scores[k] for k in weights)

    # A single overwhelming channel should not be averaged into silence.
    strongest = max(scores["ncc"], scores["ssim"], scores["edge_correlation"])
    if strongest > 0.90 and confidence > 0.45:
        confidence = min(1.0, confidence * 1.15)

    # --- presence vetoes ----------------------------------------------------
    if signals["ncc"] >= VETO.decisive_ncc:
        confidence = min(confidence, VETO.decisive_cap)
    elif (
        signals["ncc"] >= VETO.strong_ncc
        and signals["edge_correlation"] >= VETO.strong_edge_correlation
    ):
        confidence = min(confidence, VETO.strong_cap)
    elif (
        signals["ncc"] >= VETO.good_ncc
        and signals["edge_correlation"] >= VETO.good_edge_correlation
        and signals["colour_distance"] <= VETO.good_colour
    ):
        confidence = min(confidence, VETO.good_cap)

    # A featureless ROI on both sides carries no information. Reporting it as
    # missing would be a guess dressed up as a measurement.
    if (
        signals["ref_contrast"] < 3.0
        and signals["test_contrast"] < 3.0
        and signals["intensity_shift"] < 0.05
    ):
        confidence = min(confidence, 0.10)

    return float(np.clip(confidence, 0.0, 1.0))


def _build_evidence(scores: dict[str, float]) -> tuple[list[dict[str, Any]], str]:
    weights = WEIGHTS.as_dict()
    evidence = [
        {
            "key": key,
            "label": SIGNAL_LABELS[key][0],
            "score": round(scores[key], 4),
            "weight": weights[key],
            "contribution": round(scores[key] * weights[key], 4),
        }
        for key in weights
    ]
    evidence.sort(key=lambda item: item["contribution"], reverse=True)

    firing = [e for e in evidence if e["score"] >= 0.5]
    if firing:
        reason = "; ".join(SIGNAL_LABELS[e["key"]][1] for e in firing[:3])
        reason = reason[0].upper() + reason[1:] + "."
    else:
        reason = "Matches the reference board."
    return evidence, reason


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def inspect(
    reference_bgr: np.ndarray,
    parts: Iterable[dict[str, Any]],
    test_bgr: np.ndarray,
    sensitivity: str = DEFAULT_SENSITIVITY,
    keep_aligned_image: bool = True,
    test_mask: np.ndarray | None = None,
) -> InspectionResult:
    """Run a full inspection of one test photograph against the reference."""
    preset = SENSITIVITY_PRESETS.get(sensitivity, SENSITIVITY_PRESETS[DEFAULT_SENSITIVITY])
    part_list = list(parts)

    alignment = align(reference_bgr, test_bgr)
    warped = warp_to_reference(test_bgr, alignment.matrix, reference_bgr.shape)

    mask = board_mask(reference_bgr)

    # Keep any reflection out of the exposure match. A highlight is a small
    # patch of very bright pixels, and the whole-image statistics are not
    # blind to it: it drags the median and the spread, so the correction
    # computed from them is wrong everywhere *else*. Measured, that indirect
    # damage was the whole story. The parts a reflection made the engine call
    # missing were not the parts underneath it; they were ordinary parts
    # across the board, mis-levelled because a highlight in one corner had
    # set the exposure for all of them.
    bare = board_mask(reference_bgr).copy()
    rh, rw = reference_bgr.shape[:2]
    for part in part_list:
        px, py, pw, ph = _clamp_bbox(part["bbox"], rw, rh)
        bare[py:py + ph, px:px + pw] = 0
    highlight = P.light_anomaly_mask(reference_bgr, warped, bare, GLARE_RATIO)
    level_mask = cv2.bitwise_and(mask, cv2.bitwise_not(highlight))
    if np.count_nonzero(level_mask) < 0.2 * max(np.count_nonzero(mask), 1):
        level_mask = mask          # nothing trustworthy left; use what there is
    aligned, photometric = match_levels(warped, reference_bgr, level_mask)

    # Equalise focus before measuring anything. ``reference_for_measurement``
    # may be a blurred copy; the untouched ``reference_bgr`` is still what the
    # operator sees in the UI.
    reference_for_measurement, aligned_for_measurement, sharpness = match_sharpness(
        reference_bgr, aligned
    )

    # How much of each part the photograph actually shows.
    visible = coverage_mask(
        test_bgr.shape, alignment.matrix, reference_bgr.shape, test_mask
    )
    height, width = reference_bgr.shape[:2]
    coverages: list[float] = []
    for part in part_list:
        px, py, pw, ph = _clamp_bbox(part["bbox"], width, height)
        region = visible[py:py + ph, px:px + pw]
        coverages.append(float(np.count_nonzero(region)) / max(region.size, 1))

    # Where a reflection has washed the board out. Measured on the
    # illumination alone, so printed detail and components do not register.
    glare = highlight
    glare_board_fraction = float(np.count_nonzero(glare)) / max(glare.size, 1)
    raw: list[tuple[dict[str, float], dict[str, float], tuple[int, int]]] = []
    for part in part_list:
        raw.append(
            _analyse_part(reference_for_measurement, aligned_for_measurement, part["bbox"])
        )

    raw_confidences = [_fuse(signals, scores) for signals, scores, _ in raw]

    # --- scene compensation -------------------------------------------------
    compensation = 0.0
    judged = [c for c, cov in zip(raw_confidences, coverages) if cov >= MIN_PART_COVERAGE]
    if SCENE.enabled and len(judged) >= SCENE.min_parts:
        median = float(np.median(judged))
        if median > SCENE.trigger_median:
            compensation = min(
                SCENE.max_subtraction,
                SCENE.strength * (median - SCENE.trigger_median),
            )

    # --- this photograph's own noise floor ----------------------------------
    # A hand-held photo brings several nuisances at once, and they stack: each
    # one on its own leaves the engine well inside its margins, but together
    # the scores of present parts drift upward and some cross the fixed line.
    # The ordering survives, a genuinely missing part still outscores every
    # present one on nine boards in ten, so the useful question is not "is
    # this above 0.52" but "does this stand out from the rest of this photo".
    #
    # What it must not do is hide anything. Raising the line to decide
    # present/missing buys precision by missing real removals, and for an
    # inspection tool that is the wrong way round: a false alarm costs a
    # second look, a missed removal ships a faulty board. So the floor
    # separates *confident* from *worth checking* instead. Everything the
    # fixed threshold would have flagged is still flagged; what changes is
    # whether it is stated as a fact or put in front of the operator.
    confident_threshold = preset.missing_threshold
    if ADAPTIVE_THRESHOLD_K > 0 and len(judged) >= SCENE.min_parts:
        middle = float(np.median(judged))
        spread = 1.4826 * float(np.median(np.abs(np.asarray(judged) - middle)))
        floor = middle + ADAPTIVE_THRESHOLD_K * spread
        confident_threshold = float(np.clip(
            max(confident_threshold, floor),
            preset.missing_threshold,
            ADAPTIVE_THRESHOLD_CAP,
        ))

    warnings = list(alignment.warnings)

    unseen_count = sum(1 for c in coverages if c < MIN_PART_COVERAGE)
    if unseen_count:
        warnings.append(
            f"This photo shows about {alignment.coverage * 100:.0f}% of the reference "
            f"board, so {unseen_count} of {len(part_list)} named part(s) could not be "
            "checked. They are listed as not visible rather than guessed at."
        )

    if compensation > 0.01:
        warnings.append(
            "Every part looked slightly different from the reference, which "
            "usually means the photo changed rather than the board. "
            f"Scores were reduced by {compensation:.2f} to compensate."
        )
    if photometric["drift"] > 0.55:
        warnings.append(
            "Lighting differs noticeably from the reference photo. "
            "Results are corrected for it, but re-shooting the reference "
            "under current lighting will be more reliable."
        )

    if glare_board_fraction >= GLARE_BOARD_FRACTION:
        warnings.append(
            f"A reflection covers about {glare_board_fraction * 100:.0f}% of this "
            "photo. Parts under it cannot be judged, because a highlight hides a "
            "fitted component and an empty footprint equally well. Re-shoot "
            "without the flash, or move the lamp off the board's axis."
        )

    verdicts: list[PartVerdict] = []
    for part, (signals, scores, offset), raw_confidence, coverage in zip(
        part_list, raw, raw_confidences, coverages
    ):
        confidence = float(np.clip(raw_confidence - compensation, 0.0, 1.0))
        evidence, reason = _build_evidence(scores)

        if coverage < MIN_PART_COVERAGE:
            state = STATE_NOT_VISIBLE
            confidence = 0.0
            reason = (
                "This part is outside the photograph, so it was not checked."
                if coverage < 0.02
                else f"Only {coverage * 100:.0f}% of this part is in the photograph, "
                     "which is too little to judge."
            )
        elif confidence >= confident_threshold:
            state = STATE_MISSING
        elif confidence >= preset.missing_threshold:
            # Over the fixed line, but not clear of what this photograph is
            # doing to every other part on the board. Still shown, stated as
            # a thing to check rather than a thing that is so.
            state = STATE_REVIEW
            reason = (
                "This scores as missing, but this photo is making a lot of parts "
                "look altered, so it does not stand out enough to be called. "
                "Check it, or re-shoot the board more evenly lit and square on."
            )
        elif confidence >= preset.review_threshold:
            state = STATE_REVIEW
        else:
            state = STATE_PRESENT
            reason = "Matches the reference board."

        bbox = list(_clamp_bbox(part["bbox"], reference_bgr.shape[1], reference_bgr.shape[0]))
        verdicts.append(
            PartVerdict(
                part_id=str(part.get("id", "")),
                name=str(part.get("name", "Unnamed part")),
                bbox=bbox,
                state=state,
                confidence=round(confidence, 4),
                coverage=round(coverage, 4),
                raw_confidence=round(raw_confidence, 4),
                offset=[int(offset[0]), int(offset[1])],
                signals={k: round(float(v), 4) for k, v in signals.items()},
                scores={k: round(float(v), 4) for k, v in scores.items()},
                evidence=evidence,
                reason=reason,
            )
        )

    return InspectionResult(
        verdicts=verdicts,
        alignment=alignment,
        photometric={k: round(float(v), 4) for k, v in photometric.items()},
        sharpness={k: round(float(v), 4) for k, v in sharpness.items()},
        scene_compensation=round(compensation, 4),
        sensitivity=preset.key,
        thresholds={
            "missing": preset.missing_threshold,
            "review": preset.review_threshold,
        },
        warnings=warnings,
        aligned_image=aligned if keep_aligned_image else None,
    )


# --------------------------------------------------------------------------
# Crops for the detail view
# --------------------------------------------------------------------------


def crop_pair(
    reference_bgr: np.ndarray,
    aligned_bgr: np.ndarray,
    verdict: PartVerdict,
    context: float = 0.35,
    target_px: int = 220,
) -> tuple[np.ndarray, np.ndarray]:
    """Matching reference / test crops around a part, with a little context."""
    height, width = reference_bgr.shape[:2]
    x, y, w, h = verdict.bbox
    pad = int(round(max(w, h) * context)) + 6

    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(width, x + w + pad), min(height, y + h + pad)

    ref_crop = reference_bgr[y0:y1, x0:x1].copy()
    test_crop = aligned_bgr[y0:y1, x0:x1].copy()

    scale = target_px / max(ref_crop.shape[0], ref_crop.shape[1], 1)
    if scale > 1.0:
        size = (int(ref_crop.shape[1] * scale), int(ref_crop.shape[0] * scale))
        interp = cv2.INTER_NEAREST if scale > 3 else cv2.INTER_CUBIC
        ref_crop = cv2.resize(ref_crop, size, interpolation=interp)
        test_crop = cv2.resize(test_crop, size, interpolation=interp)

    return ref_crop, test_crop
