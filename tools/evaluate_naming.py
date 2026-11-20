#!/usr/bin/env python3
"""Measure the optional Ollama label reader against ground truth.

The naming assistant is a convenience, but "convenience" is not a reason to
ship it unmeasured. This asks a local model to read the designator beside
every part on the generated board and scores the answers against the names the
generator printed there, which are known exactly.

Three numbers matter, and the third is the important one:

* **correct**   it read the designator that is actually printed;
* **declined**  it said it could not tell, and the operator types the name;
* **wrong**     it produced a confident designator that is not the one there.

A wrong answer is far worse than a declined one. A declined part still shows
"Part 7", which anybody can see needs attention; a wrong one looks finished
and quietly mislabels a board. If this script ever shows a meaningful wrong
rate, the honest response is to make the parser stricter, not to publish the
accuracy figure and hope.

Usage:
    ollama pull qwen2.5vl:3b
    python tools/evaluate_naming.py --model qwen2.5vl:3b
    python tools/evaluate_naming.py --model llava:7b --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.imaging import load, to_working_size  # noqa: E402
from app.naming import status, suggest_name  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", default="samples", help="Dataset directory")
    parser.add_argument("--model", default=None, help="Override the model tag")
    parser.add_argument("--limit", type=int, default=0, help="Only the first N parts")
    parser.add_argument("--show-all", action="store_true", help="Print every part")
    args = parser.parse_args()

    state = status()
    if not state.available:
        raise SystemExit(f"Ollama unavailable: {state.reason}")
    model = args.model or state.model

    samples = Path(args.samples)
    truth = json.loads((samples / "ground_truth.json").read_text(encoding="utf-8"))
    reference = to_working_size(load(samples / truth["reference"]))

    parts = truth["parts"]
    if args.limit:
        parts = parts[: args.limit]

    print(f"Model     : {model}")
    print(f"Reference : {truth['reference']} ({reference.shape[1]}x{reference.shape[0]})")
    print(f"Parts     : {len(parts)}\n")

    correct = declined = wrong = failed = 0
    rows: list[tuple[str, str, str]] = []
    started = time.perf_counter()

    for index, part in enumerate(parts, start=1):
        expected = part["name"].upper()
        got, note = suggest_name(reference, part["bbox"], model)

        if not got and note.startswith("TIMEOUT/ERROR"):
            outcome = "failed"
            failed += 1
        elif not got:
            outcome = "declined"
            declined += 1
        elif got.upper() == expected:
            outcome = "correct"
            correct += 1
        else:
            outcome = "WRONG"
            wrong += 1

        rows.append((expected, got or "-", outcome))
        marker = {"correct": "  ", "declined": " .", "WRONG": "<-", "failed": " !"}[outcome]
        if args.show_all or outcome != "correct":
            print(f"  {marker} {index:>3}/{len(parts)}  printed {expected:<5} "
                  f"model said {got or '(declined)':<12} {outcome}")

    elapsed = time.perf_counter() - started
    total = len(parts)

    print(f"\n  {'-' * 52}")
    print(f"  correct    {correct:>3}/{total}  ({correct / total * 100:.0f}%)")
    print(f"  declined   {declined:>3}/{total}  ({declined / total * 100:.0f}%)  "
          f"left for the operator")
    print(f"  WRONG      {wrong:>3}/{total}  ({wrong / total * 100:.0f}%)  "
          f"confidently mislabelled")
    print(f"  failed     {failed:>3}/{total}  ({failed / total * 100:.0f}%)  "
          f"timed out or errored")
    print(f"  time       {elapsed:.0f}s total, {elapsed / max(total, 1):.1f}s per part")

    if wrong:
        print("\n  Wrong answers are the ones that matter. A declined part still")
        print("  reads 'Part N' and gets fixed; a wrong one looks finished.")


if __name__ == "__main__":
    main()
