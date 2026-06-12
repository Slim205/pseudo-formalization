import re

import csv
import re
from pathlib import Path

POINTS_RES = [
    re.compile(r"<points>\s*(\d+)\s+out\s+of\s+7\s*</points>", re.IGNORECASE),
    re.compile(r"<points>\s*(\d+)\s*/\s*7\s*</points>", re.IGNORECASE),
    re.compile(r"<points>\s*(\d+)\s*</points>", re.IGNORECASE),
]

import json

_REPO_ROOT = Path(__file__).resolve().parents[1]
NEW_LABELS_PATH = _REPO_ROOT / "data" / "new_labels_full.json"
if not NEW_LABELS_PATH.exists():
    NEW_LABELS_PATH = None
if NEW_LABELS_PATH is not None:
    with open(NEW_LABELS_PATH, "r", encoding="utf-8") as _f:
        NEW_LABELS = json.load(_f)
else:
    NEW_LABELS = {}


def get_points(rec, grading_id, is_corrected_label=False):
    """Return corrected Points if is_corrected_label and the grading ID was relabeled."""
    if is_corrected_label and grading_id in NEW_LABELS:
        v= NEW_LABELS[grading_id]
        return v.get("score") if isinstance(v, dict) else v
    return rec.get("Points")

from typing import Any
from typing import Callable, List, Optional


def save_json(data: Any, filepath: str) -> None:
    """
    Save Python object (list, dict, etc.) to a JSON file.
    """
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def read_json(filepath: str) -> Any:
    """
    Read JSON file and return Python object.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_score(text: str):
    """Extract X from a few accepted <points> formats."""
    if not text:
        return None
    for rx in POINTS_RES:
        m = rx.search(text)
        if m:
            return int(m.group(1))
    return None


def transform_ground_truth(p, n_class=4):
    """
    Buckets the 0-7 scale into the 4-choice scale:
    7 -> 7 | 4-6 -> 6 | 1-3 -> 1 | 0 -> 0
    """
    try:
        val = int(p)
        if n_class == 2:  # correct / incorrect
            if val == 7:
                return 1
            else:
                return 0
        elif n_class == 3:  # correct / incorrect
            if val == 7:
                return 1
            elif val == 0:
                return 0
            else:
                return 0.5
        else:
            if val == 7:
                return 7
            if 4 <= val <= 6:
                return 6
            if 1 <= val <= 3:
                return 1
            return 0
    except:
        return 0


def save_dict_to_csv_with_id(data: dict, filepath: Path):
    if not data:
        return

    # Add a new column for the keys
    fieldnames = ["Grading ID"] + list(next(iter(data.values())).keys())

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for grading_id, row in data.items():
            writer.writerow({"Grading ID": grading_id, **row})


def csv_to_dict_by_grading_id(filepath: str, limit: int = -1) -> dict:
    data = {}

    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if limit > 0 and i >= limit:
                break
            grading_id = row["Grading ID"]
            data[grading_id] = row  # entire row stored as dict

    return data


import os
import asyncio
import re
import time
import random
import pandas as pd
from pathlib import Path

import csv

import csv
from pathlib import Path
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.metrics import confusion_matrix
import numpy as np

# def compute_metrics_and_save(
#     df: dict, output: Path, n_class: int = 2,n:int=1
# ):
#     if n==1 :
#         return compute_metrics_and_save_old(df,output,n_class)
#     else :
#         return compute_metrics_and_save_n(df,output,n_class,n)


def compute_metrics_and_save(
    df: dict,
    output: Path,
    n_class: int = 2,
    k=None,
    n=1,
    is_corrected_label=True,
):
    total = len(df)
    llm_score_missing = 0
    llm_full_output_missing = 0
    correct_count = 0
    mse_sum = 0
    valid_count = 0

    csv_rows = []
    y_true = []
    y_pred = []

    for grading_id, row in df.items():
        if k:

            llm_score = row.get(f"LLM_Score_{k}")
        else:
            llm_score = row.get("LLM_Score")

        points = get_points(row, grading_id, is_corrected_label)

        llm_full_output = row.get("LLM_Full_Output", "")
        if not llm_full_output:
            llm_full_output_missing += 1
        try:
            llm_score = float(llm_score)
        except (TypeError, ValueError):
            llm_score = None
        try:
            points = float(points)
        except (TypeError, ValueError):
            points = None
        if llm_score is None:
            llm_score_missing += 1
        else:
            valid_count += 1
            ground_truth = transform_ground_truth(points, n_class=n_class)
            llm_score_transformed = transform_ground_truth(llm_score, n_class=n_class)

            # Store for confusion matrix
            y_true.append(ground_truth)
            y_pred.append(llm_score_transformed)

            if llm_score_transformed == ground_truth:
                correct_count += 1
            if points is not None:
                mse_sum += (llm_score - points) ** 2

        csv_rows.append(
            {
                "Grading ID": grading_id,
                "Problem ID": row.get("Problem ID", ""),
                "Points": points,
                "LLM_Score": llm_score,
            }
        )

    accuracy = correct_count / valid_count if valid_count > 0 else 0.0
    mse = mse_sum / valid_count if valid_count > 0 else 0.0

    print(f"Total samples: {total}")
    print(f"LLM_Score missing: {llm_score_missing}")
    print(f"LLM_Full_Output missing: {llm_full_output_missing}")
    print(f"Accuracy (bucketed): {accuracy:.2%}")
    print(f"MSE (raw scale): {mse:.4f}")

    # Save numerical summary CSV
    output.mkdir(parents=True, exist_ok=True)
    if k:
        numerical_summary_path = output / f"grading_numerical_summary_{n_class}_{k}.csv"
    else:
        numerical_summary_path = output / f"grading_numerical_summary_{n_class}.csv"
    fieldnames = ["Grading ID", "Problem ID", "Points", "LLM_Score"]
    with open(numerical_summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in csv_rows:
            writer.writerow(row)
    print(f"📊 Numerical Output saved: '{numerical_summary_path}'")

    # --- Confusion Matrix ---
    # --- Confusion Matrix with custom labels ---
    if y_true and y_pred:
        # cm = confusion_matrix(y_true, y_pred, labels=list(range(n_class)))
        plt.figure(figsize=(6, 5))
        # print(y_true)
        # return
        # Map 0 -> Incorrect, 1 -> Correct
        if n_class == 2:
            label_mapping = {0: "Incorrect", 1: "Correct"}
        if n_class == 4:
            label_mapping = {0: "Incorrect", 1: "Partial", 6: "Almost", 7: "Correct"}
        unique_labels = sorted(set(y_true) | set(y_pred))  # get all unique class codes

        cm = confusion_matrix(y_true, y_pred, labels=unique_labels)
        plt.figure(figsize=(6, 5))

        # Map the actual codes to readable labels
        x_labels = [label_mapping.get(lbl, str(lbl)) for lbl in unique_labels]
        y_labels = [label_mapping.get(lbl, str(lbl)) for lbl in unique_labels]

        sns.heatmap(
            cm,
            annot=True,
            fmt="d",
            cmap="Blues",
            xticklabels=x_labels,
            yticklabels=y_labels,
        )
        plt.xlabel("Predicted")
        plt.ylabel("Actual")
        plt.title(f"Confusion Matrix")  # | Prompt {label_prompt} Ref
        if k:
            confusion_matrix_path = (
                output / f"grading_confusion_matrix_{n_class}_{k}.png"
            )

        else:
            confusion_matrix_path = (
                output / f"grading_confusion_matrix_{n_class}_{n}.png"
            )
        plt.savefig(confusion_matrix_path)
        plt.close()
        print(f"📊 Confusion Matrix saved: '{confusion_matrix_path}'")

        compute_classwise_accuracy(df, n_class=2)
        compute_classwise_accuracy(df, n_class=4)


from collections import defaultdict


def compute_classwise_accuracy(df: dict, n_class: int = 4):
    """
    Computes percentage of correct predictions per class.

    df: dict of grading_id -> row (must have 'LLM_Score' and 'Points')
    n_class: number of classes
    """
    class_counts = defaultdict(int)  # total elements per class
    correct_counts = defaultdict(int)  # correct predictions per class

    for row in df.values():
        llm_score = row.get("LLM_Score")
        points = row.get("Points")
        if llm_score is None or points is None:
            continue

        true_class = transform_ground_truth(points, n_class=n_class)
        pred_class = transform_ground_truth(llm_score, n_class=n_class)

        class_counts[true_class] += 1
        if pred_class == true_class:
            correct_counts[true_class] += 1

    # Compute per-class accuracy
    per_class_accuracy = {}
    for cls in class_counts:
        per_class_accuracy[cls] = (
            correct_counts[cls] / class_counts[cls] if class_counts[cls] > 0 else 0.0
        )

    # Print results
    print(f"Per-class accuracy (n_class={n_class}):")
    for cls, acc in per_class_accuracy.items():
        print(f"Class {cls}: {acc:.2%} ({correct_counts[cls]}/{class_counts[cls]})")

    return per_class_accuracy


def summarize_problem_accuracy(df: dict, n_class: int = 2, output_csv: str = None):
    """
    Summarize per problem:
    - total number of proofs
    - percentage of correct proofs (ground truth)
    - percentage of correct predicted proofs

    df: dict of grading_id -> row, must contain 'Points', 'LLM_Score', 'Problem ID'
    n_class: number of classes (2 for binary)
    output_csv: optional path to save CSV
    """
    problem_summary = defaultdict(dict)

    for row in df.values():
        problem_id = row.get("Problem ID", "Unknown")
        points = row.get("Points")
        llm_score = row.get("LLM_Score")
        if points is None or llm_score is None:
            continue

        # Transform to binary class: 1 = correct, 0 = incorrect
        true_class = transform_ground_truth(points, n_class=n_class)
        pred_class = transform_ground_truth(llm_score, n_class=n_class)

        # Initialize counts
        if "total" not in problem_summary[problem_id]:
            problem_summary[problem_id]["total"] = 0
            problem_summary[problem_id]["correct_gt"] = 0
            problem_summary[problem_id]["correct_pred"] = 0

        # Update counts
        problem_summary[problem_id]["total"] += 1
        if true_class == 1:
            problem_summary[problem_id]["correct_gt"] += 1
        if pred_class == 1:
            problem_summary[problem_id]["correct_pred"] += 1

    # Print table
    print(
        f"{'Problem ID':<12} {'Total':>5} {'% Correct GT':>12} {'% Correct Pred':>15}"
    )
    for problem_id, counts in problem_summary.items():
        total = counts["total"]
        perc_gt = (counts["correct_gt"] / total * 100) if total > 0 else 0
        perc_pred = (counts["correct_pred"] / total * 100) if total > 0 else 0
        print(f"{problem_id:<12} {total:>5} {perc_gt:>12.2f}% {perc_pred:>15.2f}%")

    return problem_summary


import matplotlib.pyplot as plt


def plot_problem_accuracy(problem_summary, output_path="problem_accuracy.png"):
    """
    Create a bar plot per problem:
    - Total proofs on primary y-axis
    - % Correct GT and % Correct Pred on secondary y-axis
    """
    problem_ids = list(problem_summary.keys())
    totals = [problem_summary[pid]["total"] for pid in problem_ids]
    perc_gt = [
        problem_summary[pid]["correct_gt"] / problem_summary[pid]["total"] * 100
        for pid in problem_ids
    ]
    perc_pred = [
        problem_summary[pid]["correct_pred"] / problem_summary[pid]["total"] * 100
        for pid in problem_ids
    ]

    x = range(len(problem_ids))

    fig, ax1 = plt.subplots(figsize=(max(6, len(problem_ids) * 0.6), 5))

    # Plot total on primary y-axis
    ax1.bar(x, totals, color="lightgray", width=0.6, label="Total proofs")
    ax1.set_xlabel("Problem ID")
    ax1.set_ylabel("Total proofs", color="gray")
    ax1.tick_params(axis="y", labelcolor="gray")
    ax1.set_xticks(x)
    # ax1.set_xticklabels(problem_ids, rotation=45, ha="right")

    # Secondary y-axis for percentages
    ax2 = ax1.twinx()
    ax2.plot(x, perc_gt, color="red", marker="o", label="% Correct Proofs (Actual)")
    ax2.plot(x, perc_pred, color="blue", marker="s", label="% Correct Proofs (Pred)")
    ax2.set_ylabel("Percentage (%)")
    ax2.set_ylim(0, 100)  # percentages always 0-100

    # Combine legends
    bars_labels = ax1.get_legend_handles_labels()
    lines_labels = ax2.get_legend_handles_labels()
    ax2.legend(
        bars_labels[0] + lines_labels[0],
        bars_labels[1] + lines_labels[1],
        loc="upper left",
    )

    plt.title("Per-Problem Accuracy Summary | Without Reference")
    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    print(f"📊 Problem accuracy plot saved: {output_path}")


import random
from collections import defaultdict


def sample_theorems_with_metrics(
    df: dict,
    n_theorems: int = 15,
    max_correct: int = 5,
    max_incorrect: int = 5,
    n_class: int = 2,
    seed: int = 42,
):
    random.seed(seed)

    grouped = defaultdict(lambda: {"correct": [], "incorrect": []})

    # --- Group proofs by theorem and ground truth ---
    for grading_id, row in df.items():
        problem_id = row.get("Problem ID")
        points = row.get("Points")
        if problem_id is None or points is None:
            continue

        true_class = transform_ground_truth(points, n_class=n_class)

        if true_class == 1:
            grouped[problem_id]["correct"].append(grading_id)
        else:
            grouped[problem_id]["incorrect"].append(grading_id)

    valid_theorems = list(grouped.keys())
    if len(valid_theorems) < n_theorems:
        print(f"Warning: Only {len(valid_theorems)} theorems available.")
        n_theorems = len(valid_theorems)

    selected_theorems = random.sample(valid_theorems, n_theorems)

    sampled_ids = []

    print("\n📚 Selected Theorems Summary:\n")
    print(f"{'Problem ID':<20} {'#Correct':>10} {'#Incorrect':>12}")

    # --- Sampling ---
    for problem_id in selected_theorems:
        correct_ids = grouped[problem_id]["correct"]
        incorrect_ids = grouped[problem_id]["incorrect"]

        selected_correct = random.sample(
            correct_ids, min(len(correct_ids), max_correct)
        )
        selected_incorrect = random.sample(
            incorrect_ids, min(len(incorrect_ids), max_incorrect)
        )

        sampled_ids.extend(selected_correct + selected_incorrect)

        print(
            f"{problem_id:<20} {len(selected_correct):>10} {len(selected_incorrect):>12}"
        )

    # --- Compute accuracy on sampled subset ---
    total = 0
    correct_predictions = 0

    correct_subset_total = 0
    correct_subset_correct_pred = 0

    incorrect_subset_total = 0
    incorrect_subset_correct_pred = 0

    for gid in sampled_ids:
        row = df[gid]
        points = row.get("Points")
        llm_score = row.get("LLM_Score")

        if points is None or llm_score is None:
            continue

        true_class = transform_ground_truth(points, n_class=n_class)
        pred_class = transform_ground_truth(llm_score, n_class=n_class)

        total += 1
        if pred_class == true_class:
            correct_predictions += 1

        if true_class == 1:
            correct_subset_total += 1
            if pred_class == 1:
                correct_subset_correct_pred += 1
        else:
            incorrect_subset_total += 1
            if pred_class == 0:
                incorrect_subset_correct_pred += 1

    overall_acc = correct_predictions / total if total else 0
    correct_acc = (
        correct_subset_correct_pred / correct_subset_total
        if correct_subset_total
        else 0
    )
    incorrect_acc = (
        incorrect_subset_correct_pred / incorrect_subset_total
        if incorrect_subset_total
        else 0
    )

    print("\n📊 Accuracy on Sampled Subset:\n")
    print(f"Overall accuracy: {overall_acc:.2%} ({correct_predictions}/{total})")
    print(
        f"Accuracy on CORRECT proofs: {correct_acc:.2%} ({correct_subset_correct_pred}/{correct_subset_total})"
    )
    print(
        f"Accuracy on INCORRECT proofs: {incorrect_acc:.2%} ({incorrect_subset_correct_pred}/{incorrect_subset_total})"
    )

    return sampled_ids


import csv

import csv


def save_subset_versions(sampled_df: dict, output_prefix: str):
    """
    Save two CSV files:
        1. Full version (all columns)
        2. Clean version (without LLM_Full_Output and LLM_Score)
    """

    if not sampled_df:
        print("⚠️ Empty dataset, nothing to save.")
        return

    # Get full fieldnames + add grading_id
    first_row = next(iter(sampled_df.values()))
    full_fieldnames = ["Grading ID"] + list(first_row.keys())

    # Clean version removes LLM columns
    clean_fieldnames = ["Grading ID"] + [
        k for k in first_row.keys() if k not in ["LLM_Full_Output", "LLM_Score"]
    ]

    full_path = f"{output_prefix}_full.csv"
    clean_path = f"{output_prefix}_clean.csv"

    # --- Save full version ---
    with open(full_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=full_fieldnames)
        writer.writeheader()

        for grading_id, row in sampled_df.items():
            row_out = {"Grading ID": grading_id, **row}
            writer.writerow(row_out)

    # --- Save clean version ---
    with open(clean_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=clean_fieldnames)
        writer.writeheader()

        for grading_id, row in sampled_df.items():
            filtered_row = {
                "Grading ID": grading_id,
                **{k: v for k, v in row.items() if k in clean_fieldnames},
            }
            writer.writerow(filtered_row)

    print(f"📄 Full dataset saved to: {full_path}")
    print(f"📄 Clean dataset saved to: {clean_path}")


def k_balanced_blocks(
    text: str,
    k: int,
    sep: str = "\n\n",
    *,
    length: Optional[Callable[[str], int]] = None,
    remove_empty: bool = True,
) -> List[str]:
    """
    Partition text.split(sep) into exactly k contiguous blocks (order preserved)
    so that the minimum len(block) across the k blocks is as large as possible.

    - length: optional callable to measure paragraph length; defaults to len(s) (chars).
    - remove_empty: drop empty/whitespace-only paragraphs before partitioning.

    Returns: list of k strings (blocks).
    Raises: ValueError if k <= 0 or k > number of paragraphs (after filtering).
    """
    if length is None:
        length = lambda s: len(s)

    # Split into paragraphs and optionally drop empties
    parts = text.split(sep)
    if remove_empty:
        parts = [p for p in parts if p.strip() != ""]
    n = len(parts)

    if k <= 0:
        raise ValueError("k must be >= 1")
    if n == 0:
        # No content: return k empty blocks
        return [""] * k
    if k > n:
        k = n
        # raise ValueError(f"k ({k}) cannot exceed number of non-empty segments ({n}).")

    # Precompute paragraph "weights"
    w = [length(p) for p in parts]
    sep_len = len(sep)

    # Feasibility: can we form at least k blocks with each block length >= target?
    # Length is measured on the joined block string (includes separators inside the block).
    def can(target: int) -> bool:
        count = 0
        acc = 0
        first_in_block = True
        for wi in w:
            add = wi if first_in_block else wi + sep_len
            acc += add
            if acc >= target:
                count += 1
                acc = 0
                first_in_block = True
            else:
                first_in_block = False
        return count >= k

    # Binary search the best minimal block length
    # Upper bound: putting everything in one block (all seps included)
    lo, hi = 0, sum(w) + sep_len * (n - 1)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if can(mid):
            lo = mid
        else:
            hi = mid - 1
    best_min = lo

    # Reconstruct exactly k blocks achieving min length >= best_min
    blocks: List[str] = []
    start = 0
    acc = 0
    first_in_block = True

    for i, wi in enumerate(w):
        add = wi if first_in_block else wi + sep_len
        acc += add
        remaining_parts = n - (i + 1)
        remaining_blocks_to_start = k - len(blocks) - 1

        # Cut here if we meet the target and still can leave at least one part per remaining block
        if acc >= best_min and remaining_parts >= remaining_blocks_to_start:
            blocks.append(sep.join(parts[start : i + 1]))
            start = i + 1
            acc = 0
            first_in_block = True
        else:
            first_in_block = False

    # Append remainder as the last block
    if start < n:
        blocks.append(sep.join(parts[start:]))

    # If we somehow formed more than k blocks, merge from the right
    while len(blocks) > k:
        blocks[-2] = sep.join([blocks[-2], blocks[-1]])
        blocks.pop()

    # If we formed fewer than k (shouldn't happen with the logic above), pad empties
    while len(blocks) < k:
        blocks.append("")

    return blocks
