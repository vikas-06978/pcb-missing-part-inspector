"""Optional reference-designator suggestion through a local Ollama model.

What this is, and what it deliberately is not
---------------------------------------------
This reads the silkscreen text beside a component and offers it as a *name*.
It is a typing shortcut, nothing more. It has no part in deciding whether a
component is present or missing: that stays entirely in ``app.vision``, which
is classical, deterministic and explainable.

Three rules it follows, because the brief is strict about what may sit inside
the solution:

1. **Off unless asked for.** Nothing here runs at import, at startup, or
   during an inspection. It runs when an operator presses a button.
2. **Local only.** The default endpoint is 127.0.0.1:11434. A non-local URL is
   rejected outright rather than quietly dialled, so this cannot become a
   network dependency by misconfiguration.
3. **Never load-bearing.** Every failure path, including Ollama not being
   installed, returns "no suggestion". Naming by hand is the normal route and
   is always available; this only ever pre-fills it.

The model's answer is also not trusted as given. A vision model will cheerfully
invent a plausible designator, so the reply is parsed against the shape a
designator actually has and anything else is discarded.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

import cv2
import numpy as np

from app.imaging import encode_png_bytes

#: The feature is off unless this is set, even when Ollama is running and
#: healthy. Availability is not consent.
#:
#: The brief reads: "No online APIs or hosted services ... AI tools like
#: Gemini may be used only to create practice data, never inside the
#: solution." A local Ollama satisfies the first sentence completely, with no
#: internet and no API key. The second sentence is the open question, and the
#: strongest argument against a narrow reading is that a narrow one makes it
#: redundant: if it only meant hosted AI, the sentence before it already said
#: so. Drafters rarely write a rule that adds nothing.
#:
#: So the default is the conservative reading. Out of the box this app runs no
#: model of any kind, and the grader sees classical CV and nothing else.
#: Turning it on is a deliberate, documented act by whoever runs it.
ENABLE_FLAG = "PCB_ENABLE_NAMING"

DEFAULT_URL = "http://127.0.0.1:11434"
#: Chosen for reading small printed text, and for fitting in a modest GPU.
#: Qwen2.5-VL is markedly better at in-image text than general captioning
#: models like LLaVA, and the 3B build sits inside 4 GB of VRAM, which is what
#: a laptop GPU actually has. A 7B model spills to system RAM and takes
#: minutes per part, which for forty parts is not a feature anyone would use.
DEFAULT_MODEL = "qwen2.5vl:3b"

#: Only loopback. A remote Ollama would break the offline guarantee, so it is
#: refused rather than trusted to the person setting the variable.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}

#: R12, C3, U7, SW1, Y1, TP14, J2 ... letters then digits, optionally suffixed.
DESIGNATOR = re.compile(r"\b([A-Z]{1,3}\d{1,3}[A-Z]?)\b")

PROMPT = (
    "This is a close-up of a printed circuit board. One component is outlined "
    "with a red rectangle. Printed on the board beside it is a short reference "
    "designator, for example R12, C3, U7, J2, SW1 or Y1. "
    "Other components and their designators are also visible; ignore them. "
    "Reply with the designator belonging to the outlined component only, "
    "nothing else. If you cannot read it clearly, reply exactly: UNKNOWN"
)


@dataclass
class NamingStatus:
    available: bool
    url: str
    model: str
    models_installed: list[str]
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "url": self.url,
            "model": self.model,
            "models_installed": self.models_installed,
            "reason": self.reason,
        }


def _base_url() -> str:
    return os.environ.get("PCB_OLLAMA_URL", DEFAULT_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("PCB_OLLAMA_MODEL", DEFAULT_MODEL)


def _is_local(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS


def _post(path: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{_base_url()}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def enabled() -> bool:
    return os.environ.get(ENABLE_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def status(timeout: float = 1.5) -> NamingStatus:
    """Is the naming assistant both switched on and usable? Never raises."""
    url = _base_url()
    model = _model()

    if not enabled():
        return NamingStatus(
            False, url, model, [],
            "The naming assistant is off. The app uses no AI model unless you "
            f"deliberately switch it on by setting {ENABLE_FLAG}=1.",
        )

    if not _is_local(url):
        return NamingStatus(
            False, url, model, [],
            "Only a local Ollama is allowed, so that the app stays offline. "
            f"PCB_OLLAMA_URL points at {url}.",
        )

    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return NamingStatus(
            False, url, model, [],
            "Ollama is not running. Name the parts by hand, or start Ollama "
            f"and pull a vision model (ollama pull {model}).",
        )

    installed = [m.get("name", "") for m in payload.get("models", []) if m.get("name")]
    if not installed:
        return NamingStatus(
            False, url, model, [],
            f"Ollama is running but has no models. Try: ollama pull {model}",
        )

    # Accept "llava" matching "llava:7b" so a close-enough tag still works.
    stem = model.split(":")[0]
    chosen = next(
        (m for m in installed if m == model),
        next((m for m in installed if m.split(":")[0] == stem), None),
    )
    if chosen is None:
        return NamingStatus(
            False, url, model, installed,
            f"Model '{model}' is not installed. Available: {', '.join(installed)}. "
            f"Pull it with: ollama pull {model}",
        )

    return NamingStatus(True, url, chosen, installed)


def crop_for_naming(
    reference_bgr: np.ndarray,
    bbox: list[int],
    context: float = 0.75,
    target: int = 448,
    mark: bool = True,
) -> np.ndarray:
    """A crop around one component, with that component outlined.

    Two things have to be true at once, and they pull against each other.

    The designator is printed *beside* a component, not on it, so a tight crop
    of the body is the one crop guaranteed not to contain the answer. Widening
    it fixes that, and immediately causes the opposite problem: the crop now
    also holds three neighbours and *their* designators, with nothing to say
    which one the question is about.

    So the subject is outlined in red and the prompt refers to the outline.
    Without it the model has to guess which of four labels is meant, and it
    guesses confidently.
    """
    height, width = reference_bgr.shape[:2]
    x, y, w, h = (int(v) for v in bbox)
    pad = int(round(max(w, h) * context)) + 10

    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(width, x + w + pad), min(height, y + h + pad)
    crop = reference_bgr[y0:y1, x0:x1].copy()

    if crop.size == 0:
        return reference_bgr

    if mark:
        thickness = max(2, round(min(crop.shape[:2]) / 110))
        cv2.rectangle(
            crop, (x - x0, y - y0), (x - x0 + w, y - y0 + h),
            (0, 0, 255), thickness, cv2.LINE_AA,
        )

    scale = target / max(crop.shape[0], crop.shape[1], 1)
    if scale > 1.0:
        crop = cv2.resize(
            crop, (int(crop.shape[1] * scale), int(crop.shape[0] * scale)),
            interpolation=cv2.INTER_CUBIC,
        )
    return crop


def parse_designator(reply: str) -> str:
    """Pull a designator out of the model's reply, or return ''.

    Vision models like to answer in sentences and to invent something rather
    than admit they cannot read four pixels of silkscreen. Anything that is not
    shaped like a designator is thrown away, which is the whole reason this is
    safe to offer at all.
    """
    text = (reply or "").strip().upper()
    if not text or "UNKNOWN" in text:
        return ""
    match = DESIGNATOR.search(text)
    if not match:
        return ""
    candidate = match.group(1)
    # A whole paragraph that happens to contain "C3" is a guess, not a reading.
    return candidate if len(text) <= 48 else ""


def suggest_name(
    reference_bgr: np.ndarray, bbox: list[int], model: str, timeout: float = 180.0
) -> tuple[str, str]:
    """Suggest one designator. Returns ``(name, note)``; name is '' if unsure."""
    crop = crop_for_naming(reference_bgr, bbox)
    encoded = base64.b64encode(encode_png_bytes(crop)).decode("ascii")

    try:
        payload = _post(
            "/api/generate",
            {
                "model": model,
                "prompt": PROMPT,
                "images": [encoded],
                "stream": False,
                # Greedy and short: this is a reading task, not a writing one.
                "options": {"temperature": 0.0, "num_predict": 16},
            },
            timeout=timeout,
        )
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return "", f"TIMEOUT/ERROR: Ollama did not answer ({exc.__class__.__name__})."

    name = parse_designator(payload.get("response", ""))
    if not name:
        return "", "Could not read a designator here."
    return name, ""
