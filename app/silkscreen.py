"""Read the designators printed on the board, offline, with no model server.

This is a typing shortcut and nothing more. It fills in part names; the
missing/present decision never consults it. Anything it is unsure about is
left for the operator to type, which is also what happens to every part if the
reader is not installed.

Why it is built this way
------------------------
Pointing an OCR engine at the whole board, or at a crop around each part, both
fail in the same way: the engine reads the text correctly and then it gets
attached to the wrong component. Measured on the sample board, nearly every
mistake was a neighbour's designator (``R1`` read as ``F2``, ``C6`` as ``C4``).
Dense boards put several labels within a few pixels of several parts, and
nearest-neighbour matching cannot tell which belongs to which.

A board does not place its labels at random. A CAD tool prints every designator
at the same offset from its component, so that offset can be recovered from the
board itself: take one cheap reading pass, pair every text with every nearby
part, and the offset that the most pairs agree on is this board's convention.
Nothing here is hardcoded to a particular layout; a board that prints its
labels below and to the right is handled by the same code finding a different
offset.

Knowing where a label should be then makes the second pass much easier, because
the reader can be handed the narrow strip where the text sits instead of a crop
containing a whole component. Measured on the sample board, in whole runs:

    route                                 correct   wrong   time
    vision model (qwen2.5vl:3b)            30/41      8     33 min
    OCR, crop per part                     22/41     14     40 s
    OCR, whole board in one pass           15/41     22      4 s
    OCR, learned convention, strip pass    32/41      0     41 s
    typing them by hand                    41/41      0     ~2 min

Zero wrong answers is the property that matters. A blank costs the operator a
few seconds of typing; a confidently wrong designator quietly mislabels the
report, and nothing downstream would catch it. Two further ideas were tried and
measured and are not here, because both traded correct answers for nothing:
isolating the silkscreen by colour before reading (17/41, because the glyphs
are anti-aliased into the board), and rejecting readings whose letter prefix
does not match the component's visual type (turns wrong answers into blanks but
finds no new ones).

Nine of the sample board's labels are still unread. They are the chips, whose
designators are printed across a row of bright pins that the glyphs merge into.
Removing long bright structures before reading was tried; it deletes the text
along with the pins, because at this scale they are one connected blob.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

import cv2
import numpy as np

from app import naming
from app.config import SilkscreenConfig

CONFIG = SilkscreenConfig()

#: A reference designator: a short letter prefix then a number, as printed.
DESIGNATOR = re.compile(r"^[A-Z]{1,3}\d{1,3}[A-Z]?$")

_reader = None
_reader_error = ""


@dataclass(frozen=True)
class ReaderStatus:
    available: bool
    engine: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "engine": self.engine,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Convention:
    """Where this board prints a designator, relative to its component."""

    #: Offset from the part's top-left corner to the centre of its label.
    dx: float
    dy: float
    #: Typical printed glyph height, in board pixels. Everything else is
    #: measured in these units so the reader works at any image size.
    text_height: float
    #: How many part/text pairs agreed on this offset. Low agreement means the
    #: convention was guessed from too little evidence to trust.
    votes: int


def _load_reader():
    """Import the OCR engine once. Never raises; reports why if it cannot."""
    global _reader, _reader_error
    if _reader is not None or _reader_error:
        return _reader
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        _reader_error = (
            "The label reader is not installed. Run "
            "'pip install -r requirements.txt' to add it, or type the part "
            "names in by hand."
        )
        return None
    try:
        _reader = RapidOCR()
    except Exception as exc:                       # pragma: no cover
        _reader_error = f"The label reader failed to start: {exc}"
        return None
    return _reader


def status() -> ReaderStatus:
    """Can labels be read right now? Checked when the Parts screen opens.

    Behind the same switch as the vision model, deliberately. This reader is
    a convolutional network in an ONNX file, not a hand-written algorithm, and
    the brief's "AI tools ... never inside the solution" was read
    conservatively elsewhere in this project. Reading it one way for a vision
    model and another way for this would be convenient rather than honest. The
    cost of the fence is one environment variable; the benefit is that the
    claim "out of the box this app runs no model of any kind" stays true.
    """
    if not naming.enabled():
        return ReaderStatus(
            False,
            reason=(
                "Label reading is off. The app uses no model unless you "
                "deliberately switch it on: start it with 'python run.py "
                f"--read-labels', or set {naming.ENABLE_FLAG}=1."
            ),
        )
    if _load_reader() is None:
        return ReaderStatus(False, reason=_reader_error)
    return ReaderStatus(True, engine="RapidOCR (offline, on this machine)")


def _read(image: np.ndarray, upscale: float) -> list[tuple[str, float, float, float]]:
    """Every designator-shaped string in an image, with its centre and height.

    Coordinates come back in the input image's own pixels, not the upscaled
    ones, so callers never have to undo the magnification.
    """
    reader = _load_reader()
    if reader is None or image.size == 0:
        return []
    if upscale != 1.0:
        image = cv2.resize(
            image, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC
        )
    try:
        result = reader(image)[0] or []
    except Exception:                              # pragma: no cover
        return []

    found = []
    for box, text, _confidence in result:
        clean = text.strip().upper().replace(" ", "")
        if not DESIGNATOR.match(clean):
            continue
        xs = [p[0] / upscale for p in box]
        ys = [p[1] / upscale for p in box]
        found.append((clean, sum(xs) / 4, sum(ys) / 4, max(ys) - min(ys)))
    return found


def learn_convention(
    reference: np.ndarray, boxes: Sequence[Sequence[int]]
) -> Convention | None:
    """Work out where this board prints its designators.

    One reading pass over a modestly enlarged board is enough. It will not find
    every label, and it does not need to: the offset only has to be agreed on
    by a decent minority of them.
    """
    if not boxes:
        return None

    seeds = _read(reference, CONFIG.survey_upscale)
    if len(seeds) < CONFIG.min_seeds:
        return None

    heights = sorted(h for _t, _x, _y, h in seeds if h > 0)
    text_height = float(np.median(heights)) if heights else CONFIG.fallback_text_height
    reach = text_height * CONFIG.pairing_reach
    tolerance = text_height * CONFIG.offset_tolerance

    # Pair every text with every part it could plausibly belong to. Most of
    # these pairs are wrong; the point is that the wrong ones disagree with
    # each other while the right ones all agree.
    offsets: list[tuple[float, float]] = []
    for box in boxes:
        x, y, w, h = box[0], box[1], box[2], box[3]
        span = max(w, h) + reach
        for _text, cx, cy, _h in seeds:
            dx, dy = cx - x, cy - y
            if abs(dx) < span and abs(dy) < span:
                offsets.append((dx, dy))
    if not offsets:
        return None

    best, votes = (0.0, 0.0), 0
    for hx, hy in offsets:
        agree = sum(
            1
            for dx, dy in offsets
            if abs(dx - hx) <= tolerance and abs(dy - hy) <= tolerance
        )
        if agree > votes:
            best, votes = (hx, hy), agree
    if votes < CONFIG.min_votes:
        return None
    return Convention(best[0], best[1], text_height, votes)


def read_label(
    reference: np.ndarray, box: Sequence[int], convention: Convention
) -> str:
    """Read one part's designator from the strip the convention points at.

    Returns an empty string when nothing designator-shaped is there, which is
    the honest answer and leaves the name for the operator.
    """
    height, width = reference.shape[:2]
    unit = convention.text_height
    ex = box[0] + convention.dx
    ey = box[1] + convention.dy

    half_w = unit * CONFIG.strip_half_width
    half_h = unit * CONFIG.strip_half_height
    x0, x1 = int(max(0, ex - half_w)), int(min(width, ex + half_w))
    y0, y1 = int(max(0, ey - half_h)), int(min(height, ey + half_h))
    strip = reference[y0:y1, x0:x1]
    if strip.size == 0:
        return ""

    # Enlarge so the glyphs are a comfortable size for the recogniser. Below
    # roughly eighty pixels of text height it starts inventing characters.
    upscale = max(1.0, CONFIG.strip_text_height / max(unit, 1.0))

    best, best_gap = "", float("inf")
    for text, cx, _cy, _h in _read(strip, upscale):
        gap = abs(cx + x0 - ex)
        if gap < best_gap:
            best, best_gap = text, gap
    return best


def read_labels(
    reference: np.ndarray, boxes: Sequence[Sequence[int]]
) -> list[str]:
    """Read every part's designator. Blank where the label could not be read."""
    convention = learn_convention(reference, boxes)
    if convention is None:
        return ["" for _ in boxes]
    return [read_label(reference, box, convention) for box in boxes]


def settle_conflicts(names: Iterable[str]) -> list[str]:
    """Blank out any designator claimed by more than one part.

    Two components cannot share a designator, so when several parts claim the
    same one at least one is wrong and nothing says which. Guessing between
    them would put a wrong name on the report; blanking them asks the operator
    for a few seconds of typing instead.
    """
    names = list(names)
    seen: dict[str, int] = {}
    for name in names:
        if name:
            seen[name.upper()] = seen.get(name.upper(), 0) + 1
    return ["" if name and seen[name.upper()] > 1 else name for name in names]
