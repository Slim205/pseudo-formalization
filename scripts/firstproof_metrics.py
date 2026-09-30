"""Precision / recall vs k for a firstproof_grading.py output file.

For a subset S of k rollouts of one proof, the predictions are those of every
rollout in S (a rollout whose theorem check fired contributes only the theorem
check's prediction). A ground-truth error is a true positive if any prediction
matches it; a prediction matching no ground-truth error is a false positive
(predictions are not deduplicated across rollouts); unmatched ground-truth
errors are false negatives. TP/FP/FN are summed over proofs.

Each k reports the exact mean over all C(n, k) rollout subsets (the same subset
is used for every proof). Precision and recall are means of per-subset ratios.

For Pseudo-Formalization, "calibrated" counts only block flags the calibrator
kept (the paper's method); "uncalibrated" counts every block flag.

Usage: python scripts/firstproof_metrics.py <grading_full_output.json> [--csv out.csv]
"""

import argparse
import csv
import json
from itertools import combinations
from pathlib import Path


def rollout_matches(run, calibrated):
    """Matched ground-truth index sets, one per counted prediction."""
    return [set(m) for p, m in zip(run["predictions"], run["judge"]["pred_matches"])
            if not calibrated or p.get("calibrator_kept", True)]


def score(data, sel, calibrated):
    tp = fp = fn = 0
    for entry in data.values():
        runs = {r["run_idx"]: r for r in entry["runs"]}
        n_golds = len(entry["errors"])
        matched, n_fp = set(), 0
        for i in sel:
            for m in rollout_matches(runs[i], calibrated):
                if m:
                    matched |= m
                else:
                    n_fp += 1
        tp += len(matched)
        fp += n_fp
        fn += n_golds - len(matched)
    return tp, fp, fn


def curve(data, calibrated):
    n = min(len(e["runs"]) for e in data.values())
    rows = []
    for k in range(1, n + 1):
        vals = [score(data, s, calibrated) for s in combinations(range(n), k)]
        rows.append({
            "k": k,
            "tp": sum(v[0] for v in vals) / len(vals),
            "fp": sum(v[1] for v in vals) / len(vals),
            "fn": sum(v[2] for v in vals) / len(vals),
            "precision": sum(a / (a + b) if a + b else 0.0 for a, b, _ in vals) / len(vals),
            "recall": sum(a / (a + c) if a + c else 0.0 for a, _, c in vals) / len(vals),
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    ap.add_argument("--csv", type=Path, default=None)
    a = ap.parse_args()
    data = json.loads(a.output.read_text())
    is_pf = any(r.get("blocks") is not None or "calibrator" in r
                for e in data.values() for r in e["runs"])
    series = ({"pf_calibrated": True, "pf_uncalibrated": False} if is_pf
              else {"baseline": False})

    table = []
    for name, calibrated in series.items():
        for row in curve(data, calibrated):
            table.append({"method": name, **row})
    print(f"{'method':<16} {'k':>2} {'TP':>7} {'FP':>7} {'FN':>7} {'prec':>6} {'rec':>6}")
    for r in table:
        print(f"{r['method']:<16} {r['k']:>2} {r['tp']:>7.3f} {r['fp']:>7.3f} "
              f"{r['fn']:>7.3f} {r['precision']:>6.3f} {r['recall']:>6.3f}")
    if a.csv:
        with a.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(table[0].keys()))
            w.writeheader()
            w.writerows(table)
        print(f"wrote {a.csv}")


if __name__ == "__main__":
    main()
