"""Score the label reader against ground truth, so its numbers are checkable.

The reader is a typing shortcut and can never change a verdict, but a shortcut
that is confidently wrong is worse than no shortcut, so it publishes an error
rate like everything else here.

    python tools/evaluate_labels.py
    python tools/evaluate_labels.py --samples samples --samples other/samples

Three outcomes are counted separately and they are not interchangeable:

    correct   the designator printed on the board
    WRONG     a different designator, which would mislabel the report
    blank     declined, left for the operator to type

A blank costs a few seconds of typing. A wrong answer looks entirely plausible
and is caught only by checking every name against the board, which is the work
the reader was meant to save. Treat any non-zero WRONG as a regression.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import silkscreen
from app.imaging import load, to_working_size


def score(root: pathlib.Path) -> tuple[int, int, int, float]:
    truth = json.loads((root / "ground_truth.json").read_text(encoding="utf-8"))
    parts = truth["parts"]
    board = to_working_size(load(root / "boards" / "golden.png"))
    boxes = [p["bbox"] for p in parts]

    started = time.perf_counter()
    convention = silkscreen.learn_convention(board, boxes)
    if convention is None:
        names = ["" for _ in boxes]
    else:
        names = silkscreen.settle_conflicts(
            silkscreen.read_label(board, box, convention) for box in boxes
        )
    elapsed = time.perf_counter() - started

    correct = wrong = 0
    mistakes = []
    for part, name in zip(parts, names):
        printed = part["name"].upper()
        if not name:
            mistakes.append(f"{printed} -> (blank)")
        elif name == printed:
            correct += 1
        else:
            wrong += 1
            mistakes.append(f"{printed} -> {name}  WRONG")

    print(f"\n  {root}")
    if convention is None:
        print("  No label convention found; every name is left to be typed.")
    else:
        print(
            f"  convention   label centre at part top-left "
            f"+ ({convention.dx:+.1f}, {convention.dy:+.1f}) px, "
            f"agreed by {convention.votes} pairs"
        )
        print(f"  text height  {convention.text_height:.1f} px as printed")
    blank = len(parts) - correct - wrong
    print(f"  correct      {correct}/{len(parts)}  ({correct / len(parts):.0%})")
    print(f"  WRONG        {wrong}")
    print(f"  blank        {blank}")
    print(f"  time         {elapsed:.1f} s for {len(parts)} parts")
    if mistakes:
        print("  not read:    " + "\n               ".join(mistakes))
    return correct, wrong, blank, elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples",
        action="append",
        default=None,
        help="A sample directory holding ground_truth.json and boards/golden.png. "
        "Repeatable, so the reader can be checked on more than one board.",
    )
    args = parser.parse_args()
    roots = [pathlib.Path(s) for s in (args.samples or ["samples"])]

    state = silkscreen.status()
    if not state.available and "switch it on" not in state.reason:
        print(state.reason)
        raise SystemExit(1)

    print("Label reader evaluation")
    print("=" * 56)
    totals = [0, 0, 0, 0.0]
    for root in roots:
        if not (root / "ground_truth.json").exists():
            print(f"\n  {root}: no ground_truth.json, skipped")
            continue
        correct, wrong, blank, elapsed = score(root)
        totals = [totals[0] + correct, totals[1] + wrong,
                  totals[2] + blank, totals[3] + elapsed]

    if len(roots) > 1:
        total = totals[0] + totals[1] + totals[2]
        print("\n" + "=" * 56)
        print(f"  TOTAL  correct {totals[0]}/{total}  WRONG {totals[1]}  "
              f"blank {totals[2]}  in {totals[3]:.1f} s")
    raise SystemExit(1 if totals[1] else 0)


if __name__ == "__main__":
    main()
