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


def _odd(value: int) -> int:
    return value if value % 2 else value + 1


def _opening_kernel(image_shape: tuple[int, ...]) -> np.ndarray:
    """Kernel sized to erase silkscreen lines and traces but not components.

    This is the whole trick. An early version also used a local-variance
    texture channel, reasoning that components are busier than bare laminate.
    They are -- but so are the white silkscreen outlines printed *around* every
    component and the routed traces between them, so the mask fused
    neighbouring parts into blobs and scored 3 correct boxes out of 24.

    Silkscreen and traces are thin; component bodies are not. A morphological
    opening wide enough to delete anything narrower than roughly a tenth of
    the board's short side removes the connective tissue and leaves the parts
    standing alone. Same board, 24 of 24.
    """
    short_side = min(image_shape[0], image_shape[1])
    size = int(np.clip(_odd(round(short_side / 84)), 5, 13))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))


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


def _component_mask(image_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Everything on the board that is not bare solder mask.

    Returns ``(components, board)``. Split out of the suggester so that the
    same judgement can answer a different question: not only "where might a
    part be" but "is there a part here at all".
    """
    blurred = cv2.bilateralFilter(image_bgr, 7, 60, 60)
    mask = board_mask(blurred)

    colour = _colour_distance_map(blurred, mask)
    _, foreground = cv2.threshold(colour, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    foreground = cv2.bitwise_and(foreground, mask)

    foreground = cv2.morphologyEx(
        foreground, cv2.MORPH_OPEN, _opening_kernel(image_bgr.shape), iterations=1
    )
    # A light close afterwards only fills pinholes inside a body; it is kept
    # small on purpose, because a large one re-bridges adjacent components.
    foreground = cv2.morphologyEx(
        foreground, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1,
    )
    return foreground, mask


def empty_box_fractions(
    image_bgr: np.ndarray, boxes: list[list[int]]
) -> list[float]:
    """How much of each box has something on it, as a fraction of its area.

    A box that has been dragged onto bare board is the quietest mistake this
    tool can carry. It compares bare board against bare board, so it reports
    the part present on every photograph, for ever, while the component it
    was drawn around is no longer being looked at by anything. The count of
    checked parts stays reassuring and one of them means nothing.

    This cannot be inferred from the inspection itself, which only ever sees
    agreement. It has to be noticed on the reference, by asking whether there
    is a component inside the box at all.
    """
    if not boxes:
        return []
    foreground, _ = _component_mask(image_bgr)
    height, width = foreground.shape[:2]

    out: list[float] = []
    for box in boxes:
        x, y, w, h = (int(v) for v in box[:4])
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + w), min(height, y + h)
        region = foreground[y0:y1, x0:x1]
        out.append(
            float(np.count_nonzero(region)) / region.size if region.size else 0.0
        )
    return out


def suggest_parts(image_bgr: np.ndarray) -> list[dict]:
    """Return candidate component boxes, largest first.

    Each item is ``{"bbox": [x, y, w, h], "score": float}``. Scores are a
    rough prominence ranking used only to order the list in the UI.
    """
    height, width = image_bgr.shape[:2]
    foreground, mask = _component_mask(image_bgr)
    board_area = max(int(np.count_nonzero(mask)), 1)

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
