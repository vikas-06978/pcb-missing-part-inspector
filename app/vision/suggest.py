"""Propose candidate component boxes on the reference board.

This is an *assistant*, not a detector. The operator still names every part and
is free to delete, move or add boxes -- the brief requires naming parts once,
and clicking forty boxes by hand during a demo is an unpleasant way to spend
five minutes.

The heuristic is deliberately simple and board-agnostic, because there is no
training data: anything that is not the colour of the solder mask, or that
carries far more texture than the mask does, is a candidate.
"""

from __future__ import annotations

import cv2
import numpy as np

from app.config import SUGGEST
from app.vision.photometric import board_mask


def _dominant_lab(image_lab: np.ndarray, mask: np.ndarray) -> np.ndarray:
    pixels = image_lab[mask > 0] if np.count_nonzero(mask) > 1000 else image_lab.reshape(-1, 3)
    return np.median(pixels.reshape(-1, 3), axis=0)


def _colour_distance_map(image_bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    background = _dominant_lab(lab, mask)

    # Chroma (a, b) is weighted above lightness: a shadow changes L a lot but
    # leaves the hue of the solder mask intact, while a component body is a
    # genuinely different colour.
    diff = lab - background
    distance = np.sqrt(
        0.4 * diff[:, :, 0] ** 2 + diff[:, :, 1] ** 2 + diff[:, :, 2] ** 2
    )
    return cv2.normalize(distance, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def _texture_map(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mean = cv2.boxFilter(gray, -1, (9, 9))
    mean_sq = cv2.boxFilter(gray * gray, -1, (9, 9))
    variance = np.clip(mean_sq - mean * mean, 0, None)
    return cv2.normalize(np.sqrt(variance), None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    intersection = (x1 - x0) * (y1 - y0)
    union = aw * ah + bw * bh - intersection
    return intersection / float(union) if union else 0.0


def _merge_overlapping(
    boxes: list[tuple[int, int, int, int]], threshold: float
) -> list[tuple[int, int, int, int]]:
    """Greedy merge of boxes that clearly describe the same component."""
    merged: list[tuple[int, int, int, int]] = []
    for box in sorted(boxes, key=lambda b: b[2] * b[3], reverse=True):
        for i, kept in enumerate(merged):
            if _iou(box, kept) > threshold:
                x0 = min(box[0], kept[0])
                y0 = min(box[1], kept[1])
                x1 = max(box[0] + box[2], kept[0] + kept[2])
                y1 = max(box[1] + box[3], kept[1] + kept[3])
                merged[i] = (x0, y0, x1 - x0, y1 - y0)
                break
        else:
            merged.append(box)
    return merged


def suggest_parts(image_bgr: np.ndarray) -> list[dict]:
    """Return candidate component boxes, largest first.

    Each item is ``{"bbox": [x, y, w, h], "score": float}``. Scores are a
    rough prominence ranking used only to order the list in the UI.
    """
    height, width = image_bgr.shape[:2]
    blurred = cv2.bilateralFilter(image_bgr, 7, 60, 60)

    mask = board_mask(blurred)
    board_area = max(int(np.count_nonzero(mask)), 1)

    colour = _colour_distance_map(blurred, mask)
    texture = _texture_map(blurred)

    _, colour_fg = cv2.threshold(colour, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, texture_fg = cv2.threshold(texture, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    foreground = cv2.bitwise_or(colour_fg, texture_fg)
    foreground = cv2.bitwise_and(foreground, mask)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, kernel, iterations=1)
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, kernel, iterations=2)

    count, labels, stats, _ = cv2.connectedComponentsWithStats(foreground, connectivity=8)

    min_area = SUGGEST.min_area_fraction * board_area
    max_area = SUGGEST.max_area_fraction * board_area

    candidates: list[tuple[int, int, int, int]] = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area < min_area or area > max_area:
            continue
        if w < 6 or h < 6:
            continue
        aspect = max(w / max(h, 1), h / max(w, 1))
        if aspect > SUGGEST.max_aspect_ratio:
            continue
        # Reject wispy shapes (long traces, silkscreen lines) that fill very
        # little of their own bounding box.
        if area / float(w * h) < 0.25:
            continue

        pad = SUGGEST.pad_px
        x0 = max(0, int(x) - pad)
        y0 = max(0, int(y) - pad)
        x1 = min(width, int(x + w) + pad)
        y1 = min(height, int(y + h) + pad)
        candidates.append((x0, y0, x1 - x0, y1 - y0))

    merged = _merge_overlapping(candidates, SUGGEST.merge_iou)
    merged.sort(key=lambda b: b[2] * b[3], reverse=True)
    merged = merged[: SUGGEST.max_suggestions]

    largest = max((b[2] * b[3] for b in merged), default=1)
    return [
        {
            "bbox": [int(b[0]), int(b[1]), int(b[2]), int(b[3])],
            "score": round(float(b[2] * b[3]) / float(largest), 4),
        }
        for b in merged
    ]
