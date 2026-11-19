#!/usr/bin/env python3
"""Measure the inspection engine against the generated ground truth.

Run ``tools/generate_samples.py`` first, then::

    python tools/evaluate.py                 # headline numbers
    python tools/evaluate.py --sweep         # threshold operating curve
    python tools/evaluate.py --per-part      # every decision, for debugging

The unit of evaluation is a *part decision*: for each of the 24 named parts on
each of the 10 test photographs, was the PRESENT/MISSING call correct? That is
240 decisions, which is a far more honest measure than "did the board get the
right verdict", where guessing "OK" every time already scores 40%.

Two numbers matter, and they are not symmetric:

* **Recall**  -- missed removals ship a defective board.
* **Precision** -- false alarms stop a good line and, worse, teach the
  operator to ignore the tool.

Parts landing in the REVIEW band are counted as *detections* here, because a
flagged part does get looked at. They are reported separately too, so the cost
of the review band is visible rather than hidden.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import SENSITIVITY_PRESETS, STATE_MISSING, STATE_REVIEW  # noqa: E402
from app.imaging import fit_to_reference, load, to_working_size  # noqa: E402
from app.vision import inspect  # noqa: E402


def _fmt(value: float) -> str:
    return f"{value * 100:5.1f}%"


def load_dataset(samples_dir: Path) -> tuple[dict, np.ndarray, list[dict]]:
    truth_path = samples_dir / "ground_truth.json"
    if not truth_path.exists():
        raise SystemExit(
            f"{truth_path} not found. Run: python tools/generate_samples.py"
        )
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    reference = to_working_size(load(samples_dir / truth["reference"]))
    parts = [
        {"id": f"p{i:03d}", "name": p["name"], "bbox": p["bbox"]}
        for i, p in enumerate(truth["parts"])
    ]
    return truth, reference, parts


def run_all(
    samples_dir: Path, reference: np.ndarray, parts: list[dict], truth: dict, sensitivity: str
) -> list[dict]:
    """Run the engine once per case and keep the raw confidences."""
    results = []
    for case in truth["cases"]:
        image = fit_to_reference(
            to_working_size(load(samples_dir / case["file"])), reference.shape
        )
        started = time.perf_counter()
        result = inspect(reference, parts, image, sensitivity=sensitivity,
                         keep_aligned_image=False)
        elapsed = (time.perf_counter() - started) * 1000

        results.append(
            {
                "case": case,
                "elapsed_ms": elapsed,
                "alignment": result.alignment,
                "verdicts": result.verdicts,
                "confidences": {v.name: v.confidence for v in result.verdicts},
            }
        )
    return results


def score(results: list[dict]) -> dict:
    tp = fp = fn = tn = 0
    review_hits = review_false = 0

    for entry in results:
        missing_truth = set(entry["case"]["missing"])
        for verdict in entry["verdicts"]:
            actually_missing = verdict.name in missing_truth
            flagged = verdict.state in (STATE_MISSING, STATE_REVIEW)

            if actually_missing and flagged:
                tp += 1
                if verdict.state == STATE_REVIEW:
                    review_hits += 1
            elif actually_missing and not flagged:
                fn += 1
            elif not actually_missing and flagged:
                fp += 1
                if verdict.state == STATE_REVIEW:
                    review_false += 1
            else:
                tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    board_correct = 0
    for entry in results:
        expected = set(entry["case"]["missing"])
        reported = {v.name for v in entry["verdicts"] if v.state == STATE_MISSING}
        if expected == reported:
            board_correct += 1

    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
        "review_hits": review_hits, "review_false": review_false,
        "board_exact": board_correct, "board_total": len(results),
        "decisions": tp + fp + fn + tn,
    }


def sweep(results: list[dict], truth: dict) -> None:
    """Precision/recall as the MISSING threshold moves, ignoring the review band."""
    print("\n  Operating curve (hard MISSING threshold only)")
    print("  threshold   precision     recall         F1   false alarms")
    print("  " + "-" * 58)

    for threshold in np.arange(0.25, 0.86, 0.05):
        tp = fp = fn = 0
        for entry in results:
            missing_truth = set(entry["case"]["missing"])
            for verdict in entry["verdicts"]:
                flagged = verdict.confidence >= threshold
                if verdict.name in missing_truth:
                    tp += flagged
                    fn += not flagged
                else:
                    fp += flagged
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 1.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        print(f"     {threshold:4.2f}      {_fmt(precision)}     {_fmt(recall)}   "
              f"{_fmt(f1)}       {fp:3d}")


def per_part(results: list[dict]) -> None:
    print("\n  Every decision")
    for entry in results:
        case = entry["case"]
        missing_truth = set(case["missing"])
        print(f"\n  {Path(case['file']).name}   (severity {case['severity']})")
        for verdict in sorted(entry["verdicts"], key=lambda v: -v.confidence):
            actually_missing = verdict.name in missing_truth
            correct = (verdict.state in (STATE_MISSING, STATE_REVIEW)) == actually_missing
            marker = "  " if correct else "<-"
            print(
                f"    {marker} {verdict.name:<4} {verdict.state:<8} "
                f"conf {verdict.confidence:.3f}  "
                f"ncc {verdict.signals['ncc']:+.2f}  "
                f"ssim {verdict.signals['ssim']:+.2f}  "
                f"edge {verdict.signals['edge_correlation']:+.2f}  "
                f"col {verdict.signals['colour_distance']:.2f}"
                f"{'   TRUTH: missing' if actually_missing else ''}"
            )


def worst_offenders(results: list[dict], limit: int) -> None:
    """The highest-scoring parts that are actually present, and vice versa.

    This is the list that actually drives tuning: a false alarm at 0.61 tells
    you which evidence channel is lying, and on which kind of part.
    """
    false_alarms = []
    misses = []
    for entry in results:
        missing_truth = set(entry["case"]["missing"])
        for verdict in entry["verdicts"]:
            row = (verdict.confidence, Path(entry["case"]["file"]).name,
                   entry["case"]["severity"], verdict)
            if verdict.name in missing_truth:
                misses.append(row)
            else:
                false_alarms.append(row)

    false_alarms.sort(key=lambda r: -r[0])
    misses.sort(key=lambda r: r[0])

    def show(title: str, rows: list) -> None:
        print(f"\n  {title}")
        print(f"    {'conf':>5}  {'part':<5} {'sev':>4}  signals")
        for confidence, filename, severity, verdict in rows[:limit]:
            print(
                f"    {confidence:5.3f}  {verdict.name:<5} {severity:4.2f}  "
                f"ncc {verdict.signals['ncc']:+.2f}  "
                f"ssim {verdict.signals['ssim']:+.2f}  "
                f"edge {verdict.signals['edge_correlation']:+.2f}  "
                f"col {verdict.signals['colour_distance']:.2f}  "
                f"grad {verdict.signals['gradient_log_ratio']:.2f}  "
                f"res {verdict.signals['residual']:.2f}   {filename}"
            )

    show("Highest-scoring parts that are actually PRESENT (false alarms)", false_alarms)
    show("Lowest-scoring parts that are actually MISSING (near misses)", misses)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", default="samples", help="Dataset directory")
    parser.add_argument("--sensitivity", default="balanced",
                        choices=list(SENSITIVITY_PRESETS))
    parser.add_argument("--sweep", action="store_true", help="Show the operating curve")
    parser.add_argument("--per-part", action="store_true", help="Show every decision")
    parser.add_argument("--all-presets", action="store_true",
                        help="Score every sensitivity preset")
    parser.add_argument("--worst", type=int, default=0, metavar="N",
                        help="Show the N worst false alarms and near misses")
    args = parser.parse_args()

    samples_dir = Path(args.samples)
    truth, reference, parts = load_dataset(samples_dir)

    print(f"Reference : {truth['reference']}  "
          f"({reference.shape[1]}x{reference.shape[0]}, {len(parts)} named parts)")
    print(f"Test set  : {len(truth['cases'])} photographs")

    presets = list(SENSITIVITY_PRESETS) if args.all_presets else [args.sensitivity]
    primary: list[dict] = []

    for preset in presets:
        results = run_all(samples_dir, reference, parts, truth, preset)
        if not primary:
            primary = results
        stats = score(results)
        elapsed = [r["elapsed_ms"] for r in results]
        quality = [r["alignment"].quality for r in results]

        print(f"\n  Sensitivity: {preset}")
        print(f"  {'-' * 52}")
        print(f"  part decisions     {stats['decisions']}")
        print(f"  precision          {_fmt(stats['precision'])}   "
              f"({stats['fp']} false alarms)")
        print(f"  recall             {_fmt(stats['recall'])}   "
              f"({stats['fn']} missed removals)")
        print(f"  F1                 {_fmt(stats['f1'])}")
        print(f"  boards exact       {stats['board_exact']}/{stats['board_total']}")
        print(f"  review band        {stats['review_hits']} real, "
              f"{stats['review_false']} spurious")
        print(f"  alignment quality  {np.mean(quality):.3f} "
              f"(worst {np.min(quality):.3f})")
        print(f"  time per board     {np.mean(elapsed):.0f} ms "
              f"(max {np.max(elapsed):.0f} ms)")

    if args.sweep:
        sweep(primary, truth)
    if args.per_part:
        per_part(primary)
    if args.worst:
        worst_offenders(primary, args.worst)


if __name__ == "__main__":
    main()
