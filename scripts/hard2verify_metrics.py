"""Step- and proof-level precision / recall vs k for a hard2verify_grading.py output.

Reads `grading_full_output.json` and, for each run, the per-step verdicts:
  baseline              `step_predictions`            (1 correct, 0 incorrect, -2 unparsed)
  pseudo-formalisation  `step_verification.parsed.step_verdicts`   ("yes" / "no")

Unparsed or missing verdicts are graded as WRONG, following the Hard2Verify
authors' grading (`Hard2Verify/utils.py`, `grade_step_level_responses`): an
unparsed verdict on a correct step counts as flagging it, and on an incorrect
step as missing it.

Positive class = incorrect step. Over a subset of k runs a step is predicted
incorrect if ANY of the k runs flags it (pessimistic aggregation); TP/FP/FN are
summed over all steps of all proofs. Proof level: a proof is incorrect if any
step is labelled incorrect, and predicted incorrect if any of the k runs flags
any step.

For k < n the result is averaged over `--boot` random size-k subsets per proof
(seed 42), as in the paper's figures; k = n is exact. `--exact` instead averages
over every size-k subset (the same subset for every proof).

Usage: python scripts/hard2verify_metrics.py <grading_full_output.json> [--csv out.csv] [--exact]
"""

import argparse
import csv
import json
import random
from itertools import combinations
from pathlib import Path

B = 500
SEED = 42


def _raw_verdicts(run):
    """List of 1 / 0 / -2 for one run, in step order (possibly too short/long)."""
    if "step_predictions" in run:
        return list(run["step_predictions"] or [])
    parsed = ((run.get("step_verification") or {}).get("parsed") or {})
    return [1 if v == "yes" else 0 if v == "no" else -2
            for v in (parsed.get("step_verdicts") or [])]


def _graded(run, labels):
    """(per-step predictions with 1 = correct / 0 = incorrect, proof-level
    prediction), unparsed and missing verdicts graded as wrong."""
    preds = _raw_verdicts(run)[: len(labels)]
    wrong = lambda i: 0 if labels[i] == 1 else 1  # noqa: E731
    preds = [wrong(i) if p not in (0, 1) else p for i, p in enumerate(preds)]
    proof_pred = 0 if any(p == 0 for p in preds) else 1   # from the verdicts given
    preds += [wrong(i) for i in range(len(preds), len(labels))]
    return preds, proof_pred


def _load(path):
    data = json.loads(path.read_text())
    probs = []
    for row in data.values():
        labels = [int(l) for l in row["human_labels"]]
        runs = [_graded(r, labels) for r in row.get("LLM_Full_Output") or []]
        if runs:
            probs.append((labels, runs))
    n = min(len(r) for _, r in probs)
    return probs, n


def _pr(probs, idxs_per, level):
    tp = fp = fn = 0
    for (labels, runs), idxs in zip(probs, idxs_per):
        sel = [runs[i] for i in idxs]
        if level == "step":
            for i, hl in enumerate(labels):
                gt_inc = hl != 1
                pred_inc = any(r[0][i] != 1 for r in sel)
                tp += pred_inc and gt_inc
                fp += pred_inc and not gt_inc
                fn += (not pred_inc) and gt_inc
        else:
            gt_inc = any(hl != 1 for hl in labels)
            pred_inc = any(r[1] != 1 for r in sel)
            tp += pred_inc and gt_inc
            fp += pred_inc and not gt_inc
            fn += (not pred_inc) and gt_inc
    return (tp / (tp + fp) if tp + fp else 0.0, tp / (tp + fn) if tp + fn else 0.0)


def curve(probs, n, level, boot=B, exact=False, rng=None):
    rng = rng or random.Random(SEED)
    rows = []
    for k in range(1, n + 1):
        if k == n:
            pts = [_pr(probs, [list(range(n))] * len(probs), level)]
        elif exact:
            pts = [_pr(probs, [list(s)] * len(probs), level)
                   for s in combinations(range(n), k)]
        else:
            pts = [_pr(probs, [rng.sample(range(n), k) for _ in probs], level)
                   for _ in range(boot)]
        rows.append({"level": level, "k": k,
                     "precision": sum(p for p, _ in pts) / len(pts),
                     "recall": sum(r for _, r in pts) / len(pts)})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output_json", type=Path)
    ap.add_argument("--csv", type=Path, default=None)
    ap.add_argument("--boot", type=int, default=B)
    ap.add_argument("--exact", action="store_true",
                    help="average over every size-k subset instead of bootstrapping")
    a = ap.parse_args()

    probs, n = _load(a.output_json)
    print(f"{len(probs)} proofs, {sum(len(l) for l, _ in probs)} steps, n={n} runs")
    rng = random.Random(SEED)
    table = [r for level in ("step", "proof")
             for r in curve(probs, n, level, a.boot, a.exact, rng)]
    print(f"{'level':<6} {'k':>2} {'precision':>9} {'recall':>7}")
    for r in table:
        print(f"{r['level']:<6} {r['k']:>2} {r['precision']:>9.4f} {r['recall']:>7.4f}")
    if a.csv:
        with a.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(table[0].keys()))
            w.writeheader()
            w.writerows(table)
        print(f"wrote {a.csv}")


if __name__ == "__main__":
    main()
