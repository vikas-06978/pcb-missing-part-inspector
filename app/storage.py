"""On-disk persistence for inspection projects.

A "project" is one golden board: the reference photograph, the named parts
drawn on it, and the history of inspection runs against it. Everything lives
under ``data/projects/<id>/`` as plain files -- JSON and PNG -- so that a
project can be inspected, diffed, backed up or deleted with a file manager.

No database: a single operator on a single line never has concurrent writers,
and a directory of readable files is far easier to trust and to debug on a
factory floor than an opaque binary store.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.config import DATA_DIR, DEFAULT_SENSITIVITY, PROJECTS_DIR
from app.imaging import encode_png_bytes, load, save_png

ACTIVE_POINTER = DATA_DIR / "active.json"
PROJECT_FILE = "project.json"
REFERENCE_FILE = "reference.png"
RUNS_DIR = "runs"

EXPORT_FORMAT = "pcb-inspector/project"
EXPORT_VERSION = 1


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Part:
    id: str
    name: str
    bbox: list[int]          # [x, y, w, h] in reference pixel coordinates

    @staticmethod
    def new(name: str, bbox: list[int]) -> "Part":
        return Part(id=uuid.uuid4().hex[:10], name=name, bbox=[int(v) for v in bbox])


@dataclass
class Reference:
    width: int
    height: int
    original_filename: str = ""
    uploaded_at: float = field(default_factory=time.time)


@dataclass
class Project:
    id: str
    name: str
    created_at: float
    updated_at: float
    sensitivity: str = DEFAULT_SENSITIVITY
    reference: Reference | None = None
    parts: list[Part] = field(default_factory=list)

    # ---- paths -----------------------------------------------------------

    @property
    def directory(self) -> Path:
        return PROJECTS_DIR / self.id

    @property
    def reference_path(self) -> Path:
        return self.directory / REFERENCE_FILE

    @property
    def runs_directory(self) -> Path:
        return self.directory / RUNS_DIR

    # ---- serialisation ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sensitivity": self.sensitivity,
            "reference": asdict(self.reference) if self.reference else None,
            "parts": [asdict(p) for p in self.parts],
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "Project":
        reference = payload.get("reference")
        return Project(
            id=payload["id"],
            name=payload.get("name", "Untitled board"),
            created_at=payload.get("created_at", time.time()),
            updated_at=payload.get("updated_at", time.time()),
            sensitivity=payload.get("sensitivity", DEFAULT_SENSITIVITY),
            reference=Reference(**reference) if reference else None,
            parts=[Part(**p) for p in payload.get("parts", [])],
        )


# --------------------------------------------------------------------------
# Atomic JSON helpers
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write via a temp file + replace so a crash cannot truncate a project."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# --------------------------------------------------------------------------
# Project lifecycle
# --------------------------------------------------------------------------


def create_project(name: str = "Untitled board") -> Project:
    now = time.time()
    project = Project(
        id=uuid.uuid4().hex[:12],
        name=name.strip() or "Untitled board",
        created_at=now,
        updated_at=now,
    )
    project.directory.mkdir(parents=True, exist_ok=True)
    save_project(project)
    set_active(project.id)
    return project


def save_project(project: Project) -> None:
    project.updated_at = time.time()
    _write_json(project.directory / PROJECT_FILE, project.to_dict())


def load_project(project_id: str) -> Project | None:
    payload = _read_json(PROJECTS_DIR / project_id / PROJECT_FILE)
    if payload is None:
        return None
    try:
        return Project.from_dict(payload)
    except (KeyError, TypeError):
        return None


def list_projects() -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for directory in PROJECTS_DIR.iterdir() if PROJECTS_DIR.exists() else []:
        if not directory.is_dir():
            continue
        payload = _read_json(directory / PROJECT_FILE)
        if not payload:
            continue
        projects.append(
            {
                "id": payload.get("id", directory.name),
                "name": payload.get("name", "Untitled board"),
                "updated_at": payload.get("updated_at", 0),
                "part_count": len(payload.get("parts", [])),
                "has_reference": bool(payload.get("reference")),
            }
        )
    projects.sort(key=lambda item: item["updated_at"], reverse=True)
    return projects


def delete_project(project_id: str) -> bool:
    directory = PROJECTS_DIR / project_id
    if not directory.is_dir():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    if get_active_id() == project_id:
        ACTIVE_POINTER.unlink(missing_ok=True)
    return True


# --------------------------------------------------------------------------
# Active project pointer
# --------------------------------------------------------------------------


def set_active(project_id: str) -> None:
    _write_json(ACTIVE_POINTER, {"project_id": project_id})


def get_active_id() -> str | None:
    payload = _read_json(ACTIVE_POINTER)
    return payload.get("project_id") if payload else None


def get_active() -> Project | None:
    project_id = get_active_id()
    return load_project(project_id) if project_id else None


def get_or_create_active() -> Project:
    project = get_active()
    if project is None:
        project = create_project()
    return project


# --------------------------------------------------------------------------
# Reference image
# --------------------------------------------------------------------------


def store_reference(project: Project, image: np.ndarray, original_filename: str = "") -> None:
    """Replace the golden board. Parts and run history are discarded.

    Keeping old boxes against a new photograph would silently misalign every
    ROI, which is the kind of failure that produces confident nonsense.
    """
    save_png(project.reference_path, image)
    project.reference = Reference(
        width=int(image.shape[1]),
        height=int(image.shape[0]),
        original_filename=original_filename,
    )
    project.parts = []
    clear_runs(project)
    save_project(project)


def load_reference(project: Project) -> np.ndarray:
    return load(project.reference_path)


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def new_run_id() -> str:
    return f"{int(time.time() * 1000):x}-{uuid.uuid4().hex[:6]}"


def run_directory(project: Project, run_id: str) -> Path:
    return project.runs_directory / run_id


def store_run(
    project: Project,
    run_id: str,
    payload: dict[str, Any],
    test_image: np.ndarray,
    aligned_image: np.ndarray,
    crops: dict[str, tuple[np.ndarray, np.ndarray]],
) -> None:
    directory = run_directory(project, run_id)
    directory.mkdir(parents=True, exist_ok=True)

    save_png(directory / "test.png", test_image)
    save_png(directory / "aligned.png", aligned_image)

    crops_directory = directory / "crops"
    for part_id, (ref_crop, test_crop) in crops.items():
        save_png(crops_directory / f"{part_id}-reference.png", ref_crop)
        save_png(crops_directory / f"{part_id}-test.png", test_crop)

    _write_json(directory / "result.json", payload)


def load_run(project: Project, run_id: str) -> dict[str, Any] | None:
    return _read_json(run_directory(project, run_id) / "result.json")


def list_runs(project: Project, limit: int = 60) -> list[dict[str, Any]]:
    directory = project.runs_directory
    if not directory.exists():
        return []
    runs: list[dict[str, Any]] = []
    for run_directory_path in directory.iterdir():
        payload = _read_json(run_directory_path / "result.json")
        if not payload:
            continue
        runs.append(
            {
                "run_id": payload.get("run_id", run_directory_path.name),
                "filename": payload.get("filename", ""),
                "created_at": payload.get("created_at", 0),
                "summary": payload.get("summary", {}),
            }
        )
    runs.sort(key=lambda item: item["created_at"], reverse=True)
    return runs[:limit]


def clear_runs(project: Project) -> None:
    shutil.rmtree(project.runs_directory, ignore_errors=True)


# --------------------------------------------------------------------------
# Export / import
# --------------------------------------------------------------------------


def export_project(project: Project) -> dict[str, Any]:
    """Serialise a project, reference image included, into one JSON document.

    Used to carry a configured board between machines, and to re-load a demo
    setup instantly instead of re-drawing forty boxes.
    """
    reference_b64 = ""
    if project.reference_path.exists():
        reference_b64 = base64.b64encode(project.reference_path.read_bytes()).decode("ascii")

    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": time.time(),
        "project": project.to_dict(),
        "reference_png_base64": reference_b64,
    }


def import_project(payload: dict[str, Any]) -> Project:
    if payload.get("format") != EXPORT_FORMAT:
        raise ValueError("This file is not a PCB Inspector project export.")
    if int(payload.get("version", 0)) > EXPORT_VERSION:
        raise ValueError(
            "This project was exported by a newer version of the application."
        )

    source = payload.get("project") or {}
    now = time.time()

    project = Project(
        id=uuid.uuid4().hex[:12],
        name=source.get("name", "Imported board"),
        created_at=now,
        updated_at=now,
        sensitivity=source.get("sensitivity", DEFAULT_SENSITIVITY),
        parts=[Part(**p) for p in source.get("parts", [])],
    )
    project.directory.mkdir(parents=True, exist_ok=True)

    encoded = payload.get("reference_png_base64") or ""
    if not encoded:
        raise ValueError("The export does not contain a reference image.")

    project.reference_path.write_bytes(base64.b64decode(encoded))
    image = load(project.reference_path)
    project.reference = Reference(
        width=int(image.shape[1]),
        height=int(image.shape[0]),
        original_filename=(source.get("reference") or {}).get("original_filename", ""),
    )

    save_project(project)
    set_active(project.id)
    return project


def encode_image_data_url(image: np.ndarray) -> str:
    """Inline PNG data URL — used for report exports that must stay offline."""
    return "data:image/png;base64," + base64.b64encode(encode_png_bytes(image)).decode("ascii")
