#!/usr/bin/env python3
"""Generate a synthetic PCB dataset: one golden board plus test photographs.

Why synthetic
-------------
The brief supplies no data and requires that every photo share the reference's
camera position. Public PCB datasets (DeepPCB, HRIPCB, PCB-DSLR) are built for
*defect* detection on bare boards or for pose estimation, not for
"component removed, same viewpoint" pairs -- see RESEARCH.md. Rendering the
boards ourselves gives three things no public set does:

* exact ground truth for every part, free;
* a controllable camera-nuisance model, so we can measure at what point the
  engine breaks instead of guessing;
* a reproducible benchmark that ships in the repo and needs no download.

Everything here is drawn with OpenCV primitives. No AI image generation, no
network access, no external assets.

Usage
-----
    python tools/generate_samples.py                 # default set
    python tools/generate_samples.py --seed 7        # a different board
    python tools/generate_samples.py --out samples   # output directory
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

# --------------------------------------------------------------------------
# Palette (BGR)
# --------------------------------------------------------------------------

SOLDER_MASK = (38, 104, 36)
SOLDER_MASK_LIGHT = (52, 126, 48)
COPPER_POUR = (46, 116, 42)
SILKSCREEN = (226, 230, 226)
PAD_GOLD = (118, 186, 212)
PAD_TIN = (178, 182, 186)

IC_BODY = (34, 34, 36)
IC_BODY_LIGHT = (58, 58, 62)
PIN_METAL = (176, 180, 188)

MLCC_BODY = (122, 152, 186)
RESISTOR_BODY = (28, 28, 30)
TANTALUM_BODY = (40, 150, 196)
ELECTROLYTIC_BODY = (36, 34, 32)
ELECTROLYTIC_TOP = (120, 120, 124)
CONNECTOR_BODY = (212, 214, 216)
CONNECTOR_DARK = (46, 46, 50)
CRYSTAL_BODY = (168, 172, 178)
INDUCTOR_BODY = (54, 54, 58)
LED_BODY = (196, 226, 236)


# --------------------------------------------------------------------------
# Component model
# --------------------------------------------------------------------------


@dataclass
class Component:
    name: str
    kind: str
    x: int
    y: int
    w: int
    h: int
    label: str = ""
    rotation: int = 0          # 0 or 90, used for passives
    extra: dict = field(default_factory=dict)

    @property
    def bbox(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]

    @property
    def centre(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------


def rounded_rect(
    image: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    colour: tuple[int, int, int],
    radius: int = 3,
    thickness: int = -1,
) -> None:
    radius = max(0, min(radius, w // 2, h // 2))
    if radius == 0:
        cv2.rectangle(image, (x, y), (x + w, y + h), colour, thickness, cv2.LINE_AA)
        return
    if thickness < 0:
        cv2.rectangle(image, (x + radius, y), (x + w - radius, y + h), colour, -1, cv2.LINE_AA)
        cv2.rectangle(image, (x, y + radius), (x + w, y + h - radius), colour, -1, cv2.LINE_AA)
        for cx, cy in (
            (x + radius, y + radius),
            (x + w - radius, y + radius),
            (x + radius, y + h - radius),
            (x + w - radius, y + h - radius),
        ):
            cv2.circle(image, (cx, cy), radius, colour, -1, cv2.LINE_AA)
    else:
        cv2.rectangle(image, (x, y), (x + w, y + h), colour, thickness, cv2.LINE_AA)


def shaded_body(
    image: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    colour: tuple[int, int, int],
    radius: int = 3,
) -> None:
    """A body with a top highlight and a bottom-right drop shadow.

    The shadow matters: a real component casts one, and a removed component
    does not. Rendering it keeps the synthetic difficulty honest.
    """
    shadow = image[y:y + h + 4, x:x + w + 4]
    if shadow.size:
        darkened = (shadow.astype(np.float32) * 0.62).astype(np.uint8)
        image[y:y + h + 4, x:x + w + 4] = cv2.addWeighted(shadow, 0.45, darkened, 0.55, 0)

    rounded_rect(image, x, y, w, h, colour, radius)

    highlight = tuple(min(255, int(c * 1.35) + 14) for c in colour)
    rounded_rect(image, x + 1, y + 1, w - 2, max(2, h // 6), highlight, max(1, radius - 1))


def silkscreen_text(
    image: np.ndarray,
    text: str,
    x: int,
    y: int,
    scale: float = 0.32,
    colour: tuple[int, int, int] = SILKSCREEN,
) -> None:
    cv2.putText(
        image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, colour, 1, cv2.LINE_AA
    )


def body_text(image: np.ndarray, text: str, cx: int, cy: int, scale: float = 0.3) -> None:
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    cv2.putText(
        image,
        text,
        (cx - tw // 2, cy + th // 2),
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (208, 212, 214),
        1,
        cv2.LINE_AA,
    )


# --------------------------------------------------------------------------
# Substrate
# --------------------------------------------------------------------------


def make_substrate(width: int, height: int, rng: np.random.Generator) -> np.ndarray:
    board = np.zeros((height, width, 3), dtype=np.uint8)
    board[:] = SOLDER_MASK

    # Large-scale mottling: resin is not a flat colour.
    mottle = rng.normal(0.0, 1.0, (height // 16, width // 16, 1)).astype(np.float32)
    mottle = cv2.resize(mottle, (width, height), interpolation=cv2.INTER_CUBIC)
    mottle = cv2.GaussianBlur(mottle, (0, 0), 9)[:, :, None] * 7.0

    # Fine woven-glass texture.
    weave = rng.normal(0.0, 2.6, (height, width, 1)).astype(np.float32)

    board = np.clip(board.astype(np.float32) + mottle + weave, 0, 255).astype(np.uint8)
    return board


def draw_copper(board: np.ndarray, rng: np.random.Generator) -> None:
    height, width = board.shape[:2]

    # Ground pour with a hatched edge.
    pour = np.zeros((height, width), dtype=np.uint8)
    cv2.rectangle(pour, (24, 24), (width - 24, height - 24), 255, -1)
    cv2.rectangle(pour, (24, 24), (width - 24, height - 24), 0, 10)
    board[pour > 0] = cv2.addWeighted(
        board, 0.55, np.full_like(board, COPPER_POUR), 0.45, 0
    )[pour > 0]

    # Routed traces.
    for _ in range(90):
        x = int(rng.integers(40, width - 40))
        y = int(rng.integers(40, height - 40))
        points = [(x, y)]
        for _ in range(int(rng.integers(2, 5))):
            step = int(rng.integers(28, 130))
            if rng.random() < 0.5:
                x += step * int(rng.choice([-1, 1]))
            else:
                y += step * int(rng.choice([-1, 1]))
            x = int(np.clip(x, 30, width - 30))
            y = int(np.clip(y, 30, height - 30))
            points.append((x, y))
        cv2.polylines(
            board,
            [np.array(points, dtype=np.int32)],
            False,
            SOLDER_MASK_LIGHT,
            int(rng.integers(2, 4)),
            cv2.LINE_AA,
        )

    # Vias.
    for _ in range(120):
        cx = int(rng.integers(36, width - 36))
        cy = int(rng.integers(36, height - 36))
        cv2.circle(board, (cx, cy), 4, PAD_GOLD, -1, cv2.LINE_AA)
        cv2.circle(board, (cx, cy), 2, (24, 26, 24), -1, cv2.LINE_AA)

    # Board edge and fiducials.
    cv2.rectangle(board, (10, 10), (width - 10, height - 10), (24, 70, 26), 6, cv2.LINE_AA)
    for cx, cy in ((34, 34), (width - 34, 34), (34, height - 34), (width - 34, height - 34)):
        cv2.circle(board, (cx, cy), 9, SILKSCREEN, 2, cv2.LINE_AA)
        cv2.circle(board, (cx, cy), 4, PAD_GOLD, -1, cv2.LINE_AA)

    silkscreen_text(board, "INDUSTRY-AI  REV C", 44, height - 26, 0.44)
    silkscreen_text(board, "ASSY-7742", width - 170, height - 26, 0.42)


# --------------------------------------------------------------------------
# Footprints (always drawn, they remain when a part is removed)
# --------------------------------------------------------------------------


def draw_footprint(board: np.ndarray, component: Component) -> None:
    x, y, w, h = component.bbox
    kind = component.kind

    if kind == "ic":
        pins = component.extra.get("pins_per_side", 8)
        pad_len = max(7, int(w * 0.11))
        pad_w = max(3, int(min(w, h) / (pins * 2.6)))
        for i in range(pins):
            offset = int((i + 0.5) * h / pins)
            cv2.rectangle(board, (x - pad_len, y + offset - pad_w // 2),
                          (x + 2, y + offset + pad_w // 2), PAD_TIN, -1, cv2.LINE_AA)
            cv2.rectangle(board, (x + w - 2, y + offset - pad_w // 2),
                          (x + w + pad_len, y + offset + pad_w // 2), PAD_TIN, -1, cv2.LINE_AA)
            offset_x = int((i + 0.5) * w / pins)
            cv2.rectangle(board, (x + offset_x - pad_w // 2, y - pad_len),
                          (x + offset_x + pad_w // 2, y + 2), PAD_TIN, -1, cv2.LINE_AA)
            cv2.rectangle(board, (x + offset_x - pad_w // 2, y + h - 2),
                          (x + offset_x + pad_w // 2, y + h + pad_len), PAD_TIN, -1, cv2.LINE_AA)
        cv2.rectangle(board, (x - pad_len - 5, y - pad_len - 5),
                      (x + w + pad_len + 5, y + h + pad_len + 5), SILKSCREEN, 1, cv2.LINE_AA)
        cv2.circle(board, (x - pad_len - 11, y - pad_len - 11), 3, SILKSCREEN, -1, cv2.LINE_AA)

    elif kind == "button":
        pad = max(7, w // 6)
        for cx, cy in (
            (x, y), (x + w, y), (x, y + h), (x + w, y + h),
        ):
            cv2.circle(board, (cx, cy), pad, PAD_TIN, -1, cv2.LINE_AA)
        cv2.rectangle(board, (x - 6, y - 6), (x + w + 6, y + h + 6),
                      SILKSCREEN, 1, cv2.LINE_AA)

    elif kind == "shield":
        tab = max(8, h // 5)
        for cy in (y + h // 4, y + 3 * h // 4):
            cv2.rectangle(board, (x - 9, cy - tab // 2), (x + 3, cy + tab // 2),
                          PAD_TIN, -1, cv2.LINE_AA)
            cv2.rectangle(board, (x + w - 3, cy - tab // 2), (x + w + 9, cy + tab // 2),
                          PAD_TIN, -1, cv2.LINE_AA)
        cv2.rectangle(board, (x - 6, y - 6), (x + w + 6, y + h + 6),
                      SILKSCREEN, 1, cv2.LINE_AA)

    elif kind in {"mlcc", "resistor", "tantalum", "led", "inductor", "diode"}:
        if component.rotation == 90:
            pad_h = max(4, int(h * 0.30))
            cv2.rectangle(board, (x - 2, y - 3), (x + w + 2, y + pad_h), PAD_TIN, -1, cv2.LINE_AA)
            cv2.rectangle(board, (x - 2, y + h - pad_h), (x + w + 2, y + h + 3), PAD_TIN, -1, cv2.LINE_AA)
        else:
            pad_w = max(4, int(w * 0.30))
            cv2.rectangle(board, (x - 3, y - 2), (x + pad_w, y + h + 2), PAD_TIN, -1, cv2.LINE_AA)
            cv2.rectangle(board, (x + w - pad_w, y - 2), (x + w + 3, y + h + 2), PAD_TIN, -1, cv2.LINE_AA)
        cv2.rectangle(board, (x - 5, y - 5), (x + w + 5, y + h + 5), SILKSCREEN, 1, cv2.LINE_AA)

    elif kind == "electrolytic":
        cv2.circle(board, component.centre, w // 2 + 4, SILKSCREEN, 1, cv2.LINE_AA)
        cx, cy = component.centre
        cv2.circle(board, (cx - w // 3, cy), max(4, w // 10), PAD_TIN, -1, cv2.LINE_AA)
        cv2.circle(board, (cx + w // 3, cy), max(4, w // 10), PAD_TIN, -1, cv2.LINE_AA)

    elif kind in {"connector", "header"}:
        count = component.extra.get("pins", 8)
        for i in range(count):
            px = x + int((i + 0.5) * w / count)
            cv2.circle(board, (px, y + h - 7), 5, PAD_GOLD, -1, cv2.LINE_AA)
            cv2.circle(board, (px, y + h - 7), 2, (22, 24, 22), -1, cv2.LINE_AA)
        cv2.rectangle(board, (x - 4, y - 4), (x + w + 4, y + h + 4), SILKSCREEN, 1, cv2.LINE_AA)

    elif kind == "crystal":
        cv2.rectangle(board, (x - 4, y + 3), (x + 6, y + h - 3), PAD_TIN, -1, cv2.LINE_AA)
        cv2.rectangle(board, (x + w - 6, y + 3), (x + w + 4, y + h - 3), PAD_TIN, -1, cv2.LINE_AA)
        cv2.rectangle(board, (x - 5, y - 5), (x + w + 5, y + h + 5), SILKSCREEN, 1, cv2.LINE_AA)

    label_y = y - 10 if y > 24 else y + h + 16
    silkscreen_text(board, component.name, x, label_y, 0.34)


# --------------------------------------------------------------------------
# Bodies
# --------------------------------------------------------------------------


def draw_body(board: np.ndarray, component: Component, rng: np.random.Generator) -> None:
    x, y, w, h = component.bbox
    kind = component.kind
    cx, cy = component.centre

    if kind == "ic":
        shaded_body(board, x, y, w, h, IC_BODY, radius=4)
        cv2.circle(board, (x + 11, y + 11), 4, IC_BODY_LIGHT, -1, cv2.LINE_AA)
        pins = component.extra.get("pins_per_side", 8)
        pad_w = max(2, int(min(w, h) / (pins * 2.6)))
        for i in range(pins):
            offset = int((i + 0.5) * h / pins)
            cv2.rectangle(board, (x - 5, y + offset - pad_w // 2),
                          (x + 1, y + offset + pad_w // 2), PIN_METAL, -1, cv2.LINE_AA)
            cv2.rectangle(board, (x + w - 1, y + offset - pad_w // 2),
                          (x + w + 5, y + offset + pad_w // 2), PIN_METAL, -1, cv2.LINE_AA)
            ox = int((i + 0.5) * w / pins)
            cv2.rectangle(board, (x + ox - pad_w // 2, y - 5),
                          (x + ox + pad_w // 2, y + 1), PIN_METAL, -1, cv2.LINE_AA)
            cv2.rectangle(board, (x + ox - pad_w // 2, y + h - 1),
                          (x + ox + pad_w // 2, y + h + 5), PIN_METAL, -1, cv2.LINE_AA)
        if component.label:
            body_text(board, component.label, cx, cy - 6, 0.36)
            body_text(board, component.extra.get("sublabel", "ARM-M4"), cx, cy + 12, 0.28)

    elif kind == "mlcc":
        shaded_body(board, x, y, w, h, MLCC_BODY, radius=2)

    elif kind == "resistor":
        shaded_body(board, x, y, w, h, RESISTOR_BODY, radius=2)
        if w > 22 and h > 10:
            body_text(board, component.label or "103", cx, cy, 0.26)

    elif kind == "diode":
        shaded_body(board, x, y, w, h, (26, 26, 28), radius=2)
        stripe = max(3, w // 6)
        cv2.rectangle(board, (x + w - stripe - 1, y + 1), (x + w - 1, y + h - 1),
                      (218, 222, 226), -1, cv2.LINE_AA)

    elif kind == "button":
        shaded_body(board, x, y, w, h, (38, 38, 42), radius=3)
        cx_, cy_ = component.centre
        cv2.circle(board, (cx_, cy_), max(5, w // 3), (196, 200, 206), -1, cv2.LINE_AA)
        cv2.circle(board, (cx_, cy_), max(3, w // 5), (150, 154, 160), -1, cv2.LINE_AA)

    elif kind == "shield":
        shaded_body(board, x, y, w, h, (182, 186, 192), radius=4)
        cv2.rectangle(board, (x + 5, y + 5), (x + w - 5, y + h - 5),
                      (150, 154, 160), 1, cv2.LINE_AA)
        # Stamped slot, the giveaway detail on a real can.
        cv2.rectangle(board, (x + w // 3, y + h // 2 - 3),
                      (x + 2 * w // 3, y + h // 2 + 3), (140, 144, 150), -1, cv2.LINE_AA)

    elif kind == "tantalum":
        shaded_body(board, x, y, w, h, TANTALUM_BODY, radius=2)
        stripe = max(3, w // 7)
        cv2.rectangle(board, (x + 1, y + 1), (x + stripe, y + h - 1),
                      (226, 230, 232), -1, cv2.LINE_AA)

    elif kind == "electrolytic":
        radius = w // 2
        cv2.circle(board, (cx + 3, cy + 3), radius, (18, 18, 18), -1, cv2.LINE_AA)
        cv2.circle(board, (cx, cy), radius, ELECTROLYTIC_BODY, -1, cv2.LINE_AA)
        cv2.circle(board, (cx, cy), int(radius * 0.82), ELECTROLYTIC_TOP, -1, cv2.LINE_AA)
        cv2.line(board, (cx - int(radius * 0.7), cy), (cx + int(radius * 0.7), cy),
                 (62, 62, 66), 2, cv2.LINE_AA)
        cv2.line(board, (cx, cy - int(radius * 0.7)), (cx, cy + int(radius * 0.7)),
                 (62, 62, 66), 2, cv2.LINE_AA)
        cv2.ellipse(board, (cx, cy), (radius, radius), 0, 100, 260,
                    (228, 228, 230), max(2, radius // 5), cv2.LINE_AA)

    elif kind in {"connector", "header"}:
        colour = CONNECTOR_BODY if kind == "connector" else CONNECTOR_DARK
        shaded_body(board, x, y, w, h, colour, radius=3)
        count = component.extra.get("pins", 8)
        for i in range(count):
            px = x + int((i + 0.5) * w / count)
            cv2.rectangle(board, (px - 3, y + 5), (px + 3, y + h - 5),
                          PIN_METAL if kind == "header" else (150, 152, 156),
                          -1, cv2.LINE_AA)

    elif kind == "crystal":
        shaded_body(board, x, y, w, h, CRYSTAL_BODY, radius=6)
        cv2.rectangle(board, (x + 4, y + 4), (x + w - 4, y + h - 4),
                      (140, 144, 150), 1, cv2.LINE_AA)
        if w > 40:
            body_text(board, component.label or "16.000", cx, cy, 0.3)

    elif kind == "inductor":
        shaded_body(board, x, y, w, h, INDUCTOR_BODY, radius=4)
        cv2.ellipse(board, (cx, cy), (w // 3, h // 3), 0, 0, 360,
                    (84, 84, 90), 2, cv2.LINE_AA)

    elif kind == "led":
        cv2.circle(board, (cx + 2, cy + 2), w // 2, (20, 20, 20), -1, cv2.LINE_AA)
        shaded_body(board, x, y, w, h, LED_BODY, radius=min(w, h) // 2)
        cv2.circle(board, (cx, cy), max(2, w // 5),
                   component.extra.get("tint", (120, 200, 240)), -1, cv2.LINE_AA)

    # A faint per-part gloss so bodies are not perfectly uniform.
    gloss = rng.normal(0.0, 1.8, (h, w, 1)).astype(np.float32)
    region = board[y:y + h, x:x + w]
    if region.size:
        board[y:y + h, x:x + w] = np.clip(
            region.astype(np.float32) + gloss, 0, 255
        ).astype(np.uint8)


# --------------------------------------------------------------------------
# Board layout
# --------------------------------------------------------------------------


def build_layout() -> list[Component]:
    """A hand-placed, realistic-looking assembly of 41 components.

    Hand-placed rather than random so the board reads as a designed product,
    and so the mix is deliberate. Three properties are being engineered here,
    because each one is a different way for the inspector to fail:

    * **Size range.** A 176 px QFP and a 24 px chip resistor share one frame,
      so a single threshold has to work across an 7x span of part sizes.
    * **Colour range.** Black IC bodies, a bright metal can, white connectors,
      tan passives and a yellow tantalum. A colour metric that only works on
      dark-part-on-green-board is not actually working.
    * **Near-duplicates.** Rows of identical passives sit next to each other,
      so template matching cannot rely on a part being locally unique.
    """
    components: list[Component] = [
        # ---- top edge: connectors and user-facing hardware ----------------
        Component("J1", "connector", 120, 92, 300, 74, extra={"pins": 12}),
        Component("J2", "header", 470, 96, 150, 54, extra={"pins": 6}),
        Component("J3", "shield", 680, 90, 120, 64),
        Component("J4", "header", 860, 96, 120, 54, extra={"pins": 5}),
        Component("SW1", "button", 1040, 90, 58, 58),
        Component("F1", "resistor", 1160, 100, 56, 26, label="2A"),

        # ---- main active area ---------------------------------------------
        Component("U1", "ic", 120, 250, 176, 176, label="MCU-7742",
                  extra={"pins_per_side": 11, "sublabel": "ARM-M4"}),
        Component("Y1", "crystal", 340, 250, 76, 42, label="16.000"),
        Component("U2", "ic", 340, 330, 108, 108, label="PMIC",
                  extra={"pins_per_side": 7, "sublabel": "3V3"}),
        Component("U3", "ic", 500, 250, 90, 66, label="XCVR",
                  extra={"pins_per_side": 5, "sublabel": "CAN"}),
        Component("U5", "ic", 500, 360, 76, 76, label="QFN",
                  extra={"pins_per_side": 5, "sublabel": "IMU"}),
        Component("L1", "inductor", 640, 250, 64, 54),
        Component("D1", "led", 640, 340, 32, 32, extra={"tint": (90, 240, 120)}),
        Component("D2", "led", 640, 395, 32, 32, extra={"tint": (90, 120, 250)}),
        Component("C1", "electrolytic", 740, 250, 98, 98),
        Component("C2", "electrolytic", 860, 250, 98, 98),
        Component("U4", "ic", 1000, 250, 96, 76, label="FLASH",
                  extra={"pins_per_side": 6, "sublabel": "128M"}),
        Component("U6", "ic", 1000, 370, 70, 56, label="EE",
                  extra={"pins_per_side": 4, "sublabel": "I2C"}),
        Component("K1", "connector", 1160, 250, 130, 72, extra={"pins": 4}),
        Component("D3", "led", 1160, 360, 32, 32, extra={"tint": (70, 200, 250)}),

        # ---- passive field: rows of near-identical parts -------------------
        Component("R1", "resistor", 120, 500, 44, 24, label="103"),
        Component("R2", "resistor", 120, 545, 44, 24, label="472"),
        Component("R3", "resistor", 120, 590, 44, 24, label="100"),
        Component("R4", "resistor", 190, 500, 44, 24, label="220"),
        Component("R5", "resistor", 190, 545, 44, 24, label="332"),
        Component("R7", "resistor", 190, 590, 44, 24, label="471"),
        Component("R8", "resistor", 260, 500, 44, 24, label="152"),
        Component("R9", "resistor", 260, 545, 44, 24, label="330"),
        Component("R6", "resistor", 460, 500, 24, 44, label="10", rotation=90),

        Component("C6", "mlcc", 120, 645, 40, 26),
        Component("C7", "mlcc", 176, 645, 40, 26),
        Component("C8", "mlcc", 232, 645, 40, 26),
        Component("C9", "mlcc", 288, 645, 40, 26),
        Component("C4", "mlcc", 120, 695, 40, 26),
        Component("C5", "mlcc", 176, 695, 40, 26),
        Component("C10", "mlcc", 232, 695, 40, 26),
        Component("C11", "mlcc", 288, 695, 40, 26),

        Component("C3", "tantalum", 380, 500, 50, 30),
        Component("C12", "tantalum", 380, 550, 50, 30),
        Component("Q1", "diode", 380, 600, 42, 24),
        Component("Q2", "diode", 380, 645, 42, 24),
    ]

    _assert_no_overlap(components)
    return components


def _assert_no_overlap(components: list[Component], margin: int = 8) -> None:
    """Catch a layout typo now rather than in a confusing benchmark result.

    Two overlapping bodies would make the ground truth wrong -- removing one
    part would change the pixels of another -- which is exactly the kind of
    silent data bug that makes an evaluation meaningless.
    """
    for i, a in enumerate(components):
        for b in components[i + 1:]:
            if (
                a.x - margin < b.x + b.w and b.x - margin < a.x + a.w
                and a.y - margin < b.y + b.h and b.y - margin < a.y + a.h
            ):
                raise SystemExit(
                    f"Layout error: {a.name} {a.bbox} overlaps {b.name} {b.bbox}"
                )


def render_board(
    components: list[Component],
    present: set[str],
    seed: int,
    width: int = 1400,
    height: int = 820,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    board = make_substrate(width, height, rng)
    draw_copper(board, rng)

    for component in components:
        draw_footprint(board, component)
    for component in components:
        if component.name in present:
            draw_body(board, component, rng)

    return board


# --------------------------------------------------------------------------
# Camera nuisance model
# --------------------------------------------------------------------------


def apply_camera(
    image: np.ndarray,
    rng: np.random.Generator,
    severity: float,
    hard: float = 0.0,
    effects: frozenset[str] | None = None,
) -> np.ndarray:
    """Simulate re-photographing the board.

    ``severity`` 0 = a perfect retake, 1 = a bad day on the line: the fixture
    nudged, someone opened a roller door and changed the light, the lens is a
    little out of focus, and the frame grabber is compressing hard.

    ``hard`` adds everything that a fixed camera on a bench does *not* do, and
    a person with a phone does: holding it at an angle, standing between the
    board and the lamp, letting the flash bounce off the solder mask, and not
    waiting for focus. Those are the conditions an inspection actually meets
    away from a jig, and leaving them out of the benchmark would have meant
    reporting numbers for the easy case only.

    ``effects`` narrows ``hard`` to a subset of {tilt, dark, shadow, glare,
    blur, noise}, so each can be measured on its own. Guessing which nuisance
    is responsible for a failure has a poor record in this project; turning
    them on one at a time does not.

    It defaults to zero and draws no random numbers when it is zero, so every
    board generated before it existed is still generated byte for byte.
    """
    on = (lambda name: hard > 0 and (effects is None or name in effects))
    height, width = image.shape[:2]

    # --- geometry: the fixture is repeatable, not perfect --------------------
    angle = float(rng.normal(0.0, 0.45 * severity))
    scale = 1.0 + float(rng.normal(0.0, 0.0035 * severity))
    tx = float(rng.normal(0.0, 5.0 * severity))
    ty = float(rng.normal(0.0, 5.0 * severity))

    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, scale)
    matrix[0, 2] += tx
    matrix[1, 2] += ty
    out = cv2.warpAffine(
        image, matrix, (width, height), flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )

    # Held at an angle rather than square on. A rotation cannot express this:
    # one edge of the board ends up nearer the lens than the other, so the
    # far side is smaller and every straight line stops being parallel. This
    # is the one nuisance the alignment step was built for and had never been
    # measured against.
    if on("tilt"):
        corners = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
        reach = 0.055 * hard
        shifted = corners + np.float32([
            [rng.normal(0, reach * width), rng.normal(0, reach * height)]
            for _ in range(4)
        ])
        out = cv2.warpPerspective(
            out, cv2.getPerspectiveTransform(corners, shifted), (width, height),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE,
        )

    # --- illumination --------------------------------------------------------
    gain = 1.0 + float(rng.normal(0.0, 0.11 * severity))
    bias = float(rng.normal(0.0, 11.0 * severity))
    out = np.clip(out.astype(np.float32) * gain + bias, 0, 255)

    # Slight white-balance drift (fluorescent tubes age).
    for channel in range(3):
        out[:, :, channel] *= 1.0 + float(rng.normal(0.0, 0.025 * severity))

    # Vignette from a cheap lens.
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    radius = np.sqrt(((xx - width / 2) / (width / 2)) ** 2
                     + ((yy - height / 2) / (height / 2)) ** 2)
    vignette = 1.0 - 0.17 * severity * np.clip(radius, 0, 1.4) ** 2
    out *= vignette[:, :, None]

    # A soft diagonal light gradient, as from one overhead lamp.
    gradient = 1.0 + 0.07 * severity * ((xx / width) - 0.5 + (yy / height) - 0.5)
    out *= gradient[:, :, None]

    if hard > 0:
        # Underexposed, the way a phone meters a bright bench and leaves the
        # board itself dark. This is a real loss of information, not a shift:
        # the darkest parts of the board stop being distinguishable at all.
        if on("dark"):
            out *= 1.0 - 0.5 * hard * float(rng.random())

        # Someone leaning over the board. A soft-edged wedge across part of
        # the frame, which matters because it is local: a global exposure fix
        # cannot undo it, and the parts inside it are much darker than the
        # same parts in the reference.
        if on("shadow") and rng.random() < 0.7:
            shadow = np.zeros((height, width), np.float32)
            x0 = float(rng.uniform(-0.2, 0.8)) * width
            y0 = float(rng.uniform(-0.2, 0.8)) * height
            span = float(rng.uniform(0.35, 0.8))
            cv2.rectangle(shadow, (int(x0), int(y0)),
                          (int(x0 + span * width), int(y0 + span * height)), 1.0, -1)
            shadow = cv2.GaussianBlur(shadow, (0, 0), 0.06 * min(width, height))
            out *= (1.0 - 0.45 * hard * shadow)[:, :, None]

        # Flash or a lamp reflecting off the solder mask. Where it saturates,
        # the pixels underneath are simply gone.
        if on("glare") and rng.random() < 0.5:
            gx = float(rng.uniform(0.15, 0.85)) * width
            gy = float(rng.uniform(0.15, 0.85)) * height
            spread = float(rng.uniform(0.08, 0.2)) * width
            glare = np.exp(-(((xx - gx) ** 2 + (yy - gy) ** 2) / (2 * spread ** 2)))
            out += (170.0 * hard * glare)[:, :, None]

    out = np.clip(out, 0, 255).astype(np.uint8)

    # --- optics and sensor ---------------------------------------------------
    blur_sigma = 0.35 + 0.75 * severity * float(rng.random())
    if on("blur"):
        blur_sigma += 1.8 * hard * float(rng.random())
    if blur_sigma > 0.4:
        out = cv2.GaussianBlur(out, (0, 0), blur_sigma)

    noise_sigma = 2.2 + 5.0 * severity + (9.0 * hard if on("noise") else 0.0)
    noise = rng.normal(0.0, noise_sigma, out.shape).astype(np.float32)
    out = np.clip(out.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    quality = int(np.clip(96 - 32 * severity - (30 * hard if on("noise") else 0), 30, 98))
    ok, encoded = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if ok:
        out = cv2.imdecode(encoded, cv2.IMREAD_COLOR)

    return out


# --------------------------------------------------------------------------
# Dataset definition
# --------------------------------------------------------------------------

#: (filename stem, parts removed, nuisance severity, note shown in the docs)
CASES: list[tuple[str, list[str], float, str]] = [
    ("board_01_ok_clean",        [],                        0.15, "Good board, near-perfect retake"),
    ("board_02_missing_U2",      ["U2"],                    0.25, "One mid-size IC removed"),
    ("board_03_ok_dim",          [],                        0.60, "Good board, dimmer light and slight shift"),
    ("board_04_missing_C1_R3",   ["C1", "R3"],              0.35, "Electrolytic plus a chip resistor"),
    ("board_05_missing_small",   ["C6", "C7"],              0.30, "Two of the smallest passives"),
    ("board_06_missing_three",   ["U3", "Y1", "D1"],        0.45, "IC, crystal and an LED"),
    ("board_07_ok_harsh",        [],                        0.95, "Good board, worst-case photo"),
    ("board_08_missing_J2",      ["J2"],                    0.55, "Connector removed, harsh light"),
    ("board_09_missing_U1",      ["U1"],                    0.40, "The largest part on the board"),
    ("board_10_missing_mixed",   ["R6", "C3", "L1", "U4"],  0.70, "Four parts, harsh photo"),
    ("board_11_missing_metal",   ["J3", "SW1"],             0.50, "Bright metal can and a tactile switch"),
    ("board_12_missing_lookalike", ["R7", "C9", "Q2"],      0.65, "Parts from rows of near-identical neighbours"),
]


def build_stress_cases(
    count: int, names: list[str], seed: int
) -> list[tuple[str, list[str], float, str]]:
    """Randomised cases for a larger, harder sample.

    The curated ``CASES`` list is readable and good for demonstrating specific
    situations, but 10 boards is far too small a sample to tune thresholds
    against without fooling yourself. This produces an arbitrary number of
    cases spanning the full nuisance range, with roughly a third of the boards
    deliberately complete so that false-alarm rate is measured honestly.
    """
    rng = np.random.default_rng(seed)
    cases: list[tuple[str, list[str], float, str]] = []

    for index in range(count):
        severity = float(rng.uniform(0.10, 1.00))
        if rng.random() < 0.34:
            removed: list[str] = []
        else:
            how_many = int(rng.integers(1, 6))
            removed = sorted(
                rng.choice(names, size=min(how_many, len(names)), replace=False).tolist()
            )
        label = "ok" if not removed else f"{len(removed)}miss"
        cases.append(
            (
                f"stress_{index:03d}_{label}",
                removed,
                severity,
                "randomised stress case",
            )
        )
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="samples", help="Output directory")
    parser.add_argument("--seed", type=int, default=20261119, help="Random seed")
    parser.add_argument(
        "--stress",
        type=int,
        default=0,
        metavar="N",
        help="Generate N randomised cases instead of the curated set",
    )
    parser.add_argument(
        "--only",
        default="",
        metavar="LIST",
        help="With --hard, apply only these effects, comma separated: "
             "tilt, dark, shadow, glare, blur, noise.",
    )
    parser.add_argument(
        "--hard",
        action="store_true",
        help="Photograph the boards by hand rather than on a jig: held at an "
             "angle, underexposed, someone leaning over it, flash on the "
             "solder mask, and focus missed. Use with --stress.",
    )
    args = parser.parse_args()
    only = (frozenset(a.strip() for a in args.only.split(",") if a.strip())
            or None)

    out_dir = Path(args.out)
    boards_dir = out_dir / "boards"
    boards_dir.mkdir(parents=True, exist_ok=True)

    components = build_layout()
    all_names = {c.name for c in components}

    # The golden board gets no camera nuisance: it is the operator's careful
    # reference shot, taken once, under controlled conditions.
    golden = render_board(components, all_names, seed=args.seed)
    cv2.imwrite(str(boards_dir / "golden.png"), golden)

    ground_truth = {
        "generator": "tools/generate_samples.py",
        "seed": args.seed,
        "board_size": [golden.shape[1], golden.shape[0]],
        "reference": "boards/golden.png",
        "parts": [
            {"name": c.name, "kind": c.kind, "bbox": c.bbox} for c in components
        ],
        "cases": [],
    }

    print(f"Golden board: {boards_dir / 'golden.png'}  ({len(components)} parts)")

    cases = (
        build_stress_cases(args.stress, sorted(all_names), args.seed + 77)
        if args.stress
        else CASES
    )
    verbose = not args.stress

    for index, (stem, removed, severity, note) in enumerate(cases):
        present = all_names - set(removed)
        # Same seed as the golden board so that substrate texture, trace
        # routing and via placement are identical: the only real differences
        # are the removed parts and the camera nuisance.
        board = render_board(components, present, seed=args.seed)
        rng = np.random.default_rng(args.seed + 1000 + index)
        # Spread the hand-held difficulty across the set rather than pinning
        # every board at the worst case, so the numbers show where it starts
        # to break rather than only whether it survives the extreme.
        hard = float(rng.uniform(0.3, 1.0)) if args.hard else 0.0
        photo = apply_camera(board, rng, severity, hard, only)

        filename = f"{stem}.jpg"
        cv2.imwrite(str(boards_dir / filename), photo, [cv2.IMWRITE_JPEG_QUALITY, 94])

        ground_truth["cases"].append(
            {
                "file": f"boards/{filename}",
                "missing": removed,
                "severity": severity,
                "hard": round(hard, 3),
                "note": note,
            }
        )
        if verbose:
            label = ", ".join(removed) if removed else "nothing missing"
            print(f"  {filename:<30} severity {severity:>4.2f}  {label}")

    (out_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2), encoding="utf-8"
    )
    print(f"\nGround truth: {out_dir / 'ground_truth.json'}")
    print(f"{len(cases)} test photographs written to {boards_dir}")


if __name__ == "__main__":
    main()
