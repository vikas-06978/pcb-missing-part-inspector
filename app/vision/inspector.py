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
    RAMPS,
    SCENE,
    SENSITIVITY_PRESETS,
    STATE_MISSING,
    STATE_PRESENT,
    STATE_REVIEW,
    VETO,
    WEIGHTS,
)
from app.vision import metrics as M
from app.vision.photometric import board_mask, match_levels
from app.vision.registration import Alignment, align, warp_to_reference

# Human-readable explanation for each evidence channel when it fires.
SIGNAL_LABELS: dict[str, tuple[str, str]] = {
    "ncc": ("Template match", "the part's pattern is no longer found here"),
    "ssim": ("Structure", "the local structure no longer matches the reference"),
    "edge_iou": ("Outline", "the component outline is absent"),
    "hist_distance": ("Colour", "the colour now reads as bare board, not the part"),
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
    def is_ok(self) -> bool:
        return not self.missing and not self.review

    def summary(self) -> dict[str, Any]:
        missing = self.missing
        review = self.review
        if missing:
            status = STATE_MISSING
            headline = (
                f"{len(missing)} part missing" if len(missing) == 1
                else f"{len(missing)} parts missing"
            )
        elif review:
            status = STATE_REVIEW
            headline = (
                f"{len(review)} part needs review" if len(review) == 1
                else f"{len(review)} parts need review"
            )
        else:
            status = STATE_PRESENT
            headline = "OK"

        return {
            "status": status,
            "headline": headline,
            "total_parts": len(self.verdicts),
            "missing_count": len(missing),
            "review_count": len(review),
            "present_count": len(self.verdicts) - len(missing) - len(review),
            "missing_names": [v.name for v in missing],
            "review_names": [v.name for v in review],
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

    # --- measurements -------------------------------------------------------
    ref_small = M.ensure_min_size(ref_patch)
    test_small = M.ensure_min_size(test_patch)

    ref_gray = M.to_gray(ref_small)
    test_gray = M.to_gray(test_small)

    iou, ref_density, test_density = M.edge_iou(ref_gray, test_gray)

    signals = {
        "ncc": float(ncc),
        "ssim": M.ssim(ref_gray, test_gray),
        "edge_iou": float(iou),
        "ref_edge_density": float(ref_density),
        "test_edge_density": float(test_density),
        "hist_distance": M.hsv_hist_distance(ref_small, test_small),
        "gradient_log_ratio": M.gradient_log_ratio(ref_gray, test_gray),
        "residual": M.normalised_residual(ref_gray, test_gray),
        "intensity_shift": M.mean_intensity_shift(ref_gray, test_gray),
        "ref_contrast": float(ref_gray.std()),
        "test_contrast": float(test_gray.std()),
    }

    scores = {
        "ncc": M.inv_ramp(signals["ncc"], *RAMPS.ncc),
        "ssim": M.inv_ramp(signals["ssim"], *RAMPS.ssim),
        "edge_iou": M.inv_ramp(signals["edge_iou"], *RAMPS.edge_iou),
        "hist_distance": M.ramp(signals["hist_distance"], *RAMPS.hist_distance),
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
    strongest = max(scores["ncc"], scores["ssim"], scores["edge_iou"])
    if strongest > 0.90 and confidence > 0.45:
        confidence = min(1.0, confidence * 1.15)

    # --- presence vetoes ----------------------------------------------------
    if signals["ncc"] >= VETO.strong_ncc and signals["ssim"] >= VETO.strong_ssim:
        confidence = min(confidence, VETO.strong_cap)
    elif (
        signals["ncc"] >= VETO.good_ncc
        and signals["edge_iou"] >= VETO.good_edge_iou
        and signals["hist_distance"] <= VETO.good_hist
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
) -> InspectionResult:
    """Run a full inspection of one test photograph against the reference."""
    preset = SENSITIVITY_PRESETS.get(sensitivity, SENSITIVITY_PRESETS[DEFAULT_SENSITIVITY])
    part_list = list(parts)

    alignment = align(reference_bgr, test_bgr)
    warped = warp_to_reference(test_bgr, alignment.matrix, reference_bgr.shape)

    mask = board_mask(reference_bgr)
    aligned, photometric = match_levels(warped, reference_bgr, mask)

    raw: list[tuple[dict[str, float], dict[str, float], tuple[int, int]]] = []
    for part in part_list:
        raw.append(_analyse_part(reference_bgr, aligned, part["bbox"]))

    raw_confidences = [_fuse(signals, scores) for signals, scores, _ in raw]

    # --- scene compensation -------------------------------------------------
    compensation = 0.0
    if SCENE.enabled and len(raw_confidences) >= SCENE.min_parts:
        median = float(np.median(raw_confidences))
        if median > SCENE.trigger_median:
            compensation = min(
                SCENE.max_subtraction,
                SCENE.strength * (median - SCENE.trigger_median),
            )

    warnings = list(alignment.warnings)
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

    verdicts: list[PartVerdict] = []
    for part, (signals, scores, offset), raw_confidence in zip(part_list, raw, raw_confidences):
        confidence = float(np.clip(raw_confidence - compensation, 0.0, 1.0))

        if confidence >= preset.missing_threshold:
            state = STATE_MISSING
        elif confidence >= preset.review_threshold:
            state = STATE_REVIEW
        else:
            state = STATE_PRESENT

        evidence, reason = _build_evidence(scores)
        if state == STATE_PRESENT:
            reason = "Matches the reference board."

        bbox = list(_clamp_bbox(part["bbox"], reference_bgr.shape[1], reference_bgr.shape[0]))
        verdicts.append(
            PartVerdict(
                part_id=str(part.get("id", "")),
                name=str(part.get("name", "Unnamed part")),
                bbox=bbox,
                state=state,
                confidence=round(confidence, 4),
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
