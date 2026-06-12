"""Run a verifier (baseline or pseudo-formalisation) over the Salesforce
Hard2Verify dataset and save full per-run results to JSON.

The dataset must be pre-decrypted to ``data/hard2verify.json`` (see
``decrypt_hard2verify.py``).

Two verification methods (selected with ``--method``):
  - ``baseline``: ``Verfifierbaseline`` (one model call per run, returns a
    0-7 score that we threshold to a binary label later).
  - ``pseudo-formalisation``: ``PseudoFormalisationVerifier`` (rewrite +
    faithfulness retries + per-component verification + step meta-calibration).

Both reuse the existing IMO verifiers / prompts from ``src/verifier``.
The Hard2Verify problems are olympiad-style and the prompts already
target that style of grading; the only adaptation here is at the data
layer (concatenate ``model_response_by_step`` into a single proof and
derive a binary GT label from ``human_labels``).

Per-row data shape (matches what ``src/pipeline`` expects):

    {
        "unique_id":                str,
        "Problem":                  question (math problem text),
        "Response":                 concatenated proof (steps joined by \n\n),
        "Original_Steps":           original model_response_by_step list,
        "Points":                   7 if all human_labels == 1 else 0,
        "human_labels":             list[int],
        "human_labels_first_error_idx": int | None,
        "ground_truth_label":       "correct" | "incorrect",
        "LLM_Full_Output":          [ ... per-run results ... ],
        "LLM_Score":                pessimistic min score across runs (set after run),
    }

Outputs land at ``outputs/hard2verify-<method>/grading_full_output.json``.

Aggregation across runs (pessimistic / majority / threshold) is left to a
separate analysis step — this script only collects the independent runs.
"""

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

from src.verifier.verifier import (
    Verfifierbaseline,
    PseudoFormalisationVerifier,
)
from src.pipeline import pipeline
from src.utils import save_json, read_json


REPO_ROOT = Path(__file__).resolve().parent
DATA_PATH = REPO_ROOT / "data" / "hard2verify.json"

DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
DEFAULT_EFFORT = "medium"
DEFAULT_N_RUNS = 2
DEFAULT_METHOD = "baseline"
DEFAULT_CONCURRENCY = 16
DEFAULT_DEV_SET_SIZE = 50
DEFAULT_SEED = 42

OUTPUT_DIR_BY_METHOD = {
    "baseline": REPO_ROOT / "outputs" / "hard2verify-baseline",
    "pseudo-formalisation": REPO_ROOT / "outputs" / "hard2verify-pseudo-formalisation",
}


def _is_fully_correct(labels) -> bool:
    if not isinstance(labels, list) or len(labels) == 0:
        return False
    try:
        return all(int(l) == 1 for l in labels)
    except (TypeError, ValueError):
        return False


def _build_dataset(
    dev_set_size: int,
    seed: int,
    unique_ids: Optional[list] = None,
) -> Dict[str, Dict[str, Any]]:
    """Load the decrypted dataset, build the verifier-row dict, optionally
    take a fixed-seed random subset of size ``dev_set_size``.

    If ``unique_ids`` is provided, restrict to that set first; the dev-set
    sampling then runs on the restricted pool.
    """
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"\n\n!!! HARD2VERIFY DATA NOT FOUND: {DATA_PATH}\n"
            f"    Run `python decrypt_hard2verify.py` first to produce it.\n"
        )
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    df: Dict[str, Dict[str, Any]] = {}
    for row in raw:
        uid = row["unique_id"]
        if unique_ids and uid not in unique_ids:
            continue
        steps = row.get("model_response_by_step") or []
        if isinstance(steps, list):
            proof = "\n\n".join(str(s) for s in steps)
        else:
            proof = str(steps)
        labels = row.get("human_labels") or []
        is_correct = _is_fully_correct(labels)
        df[uid] = {
            "unique_id": uid,
            "Problem": row.get("question") or "",
            "Response": proof,
            "Original_Steps": steps if isinstance(steps, list) else [str(steps)],
            "Points": 7 if is_correct else 0,
            "human_labels": labels,
            "human_labels_first_error_idx": row.get("human_labels_first_error_idx"),
            "ground_truth_label": "correct" if is_correct else "incorrect",
        }

    if dev_set_size > 0 and dev_set_size < len(df):
        rng = random.Random(seed)
        keys = sorted(df.keys())  # sort for determinism across runs
        chosen = set(rng.sample(keys, dev_set_size))
        df = {k: v for k, v in df.items() if k in chosen}

    return df


def _build_verifier(method: str, n_runs: int, model: str, effort: str):
    if method == "baseline":
        return Verfifierbaseline(
            n=n_runs,
            max_tries=6,
            model=model,
            effort=effort,
        )
    if method == "pseudo-formalisation":
        return PseudoFormalisationVerifier(
            n=n_runs,
            n_verifications=1,
            max_tries=10,
            faithfulness_check=True,
            meta_verify=True,
            hard2verify_step_meta_verify=True,
            model=model,
            effort=effort,
        )
    raise ValueError(
        f"Unknown method {method!r}. Expected one of: "
        f"{list(OUTPUT_DIR_BY_METHOD.keys())}"
    )


def _pessimistic_min(scores):
    """Pessimistic aggregation: minimum across non-None scores."""
    vals = [s for s in scores if s is not None]
    return min(vals) if vals else None


async def main(
    method: str = DEFAULT_METHOD,
    output_dir: Optional[Path] = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    n_runs: int = DEFAULT_N_RUNS,
    concurrency: int = DEFAULT_CONCURRENCY,
    dev_set_size: int = DEFAULT_DEV_SET_SIZE,
    seed: int = DEFAULT_SEED,
    unique_ids: Optional[list] = None,
) -> Dict[str, Any]:

    if output_dir is None:
        output_dir = OUTPUT_DIR_BY_METHOD[method]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "grading_full_output.json"

    print(f"Method:        {method}")
    print(f"Model:         {model} (effort={effort})")
    print(f"Runs:          {n_runs}")
    print(f"Concurrency:   {concurrency}")
    print(f"Dev-set size:  {dev_set_size} (seed {seed})")
    print(f"Output path:   {output_path}")

    df = _build_dataset(dev_set_size, seed, unique_ids=unique_ids)
    n_correct = sum(1 for v in df.values() if v["ground_truth_label"] == "correct")
    n_incorrect = len(df) - n_correct
    print(
        f"Loaded:        {len(df)} rows  ({n_correct} correct, {n_incorrect} incorrect)"
    )

    # Resume from existing output: merge in any prior LLM_Full_Output entries.
    if output_path.exists():
        prior = read_json(output_path)
        n_resumed = 0
        for uid, prev in prior.items():
            if uid in df and prev.get("LLM_Full_Output"):
                df[uid]["LLM_Full_Output"] = prev["LLM_Full_Output"]
                if "LLM_Score" in prev:
                    df[uid]["LLM_Score"] = prev["LLM_Score"]
                n_resumed += 1
        print(f"Resumed:       {n_resumed} rows had prior runs in {output_path.name}")

    verifier = _build_verifier(method, n_runs, model, effort)

    pip = pipeline(concurrency, method=method)
    df = await pip.run(verifier, df, output_dir)

    # Pessimistic aggregation across runs (independent runs already saved by
    # the pipeline; this just sets a convenience field for downstream).
    for uid, val in df.items():
        outputs = val.get("LLM_Full_Output", []) or []
        scores = [x.get("score") for x in outputs if isinstance(x, dict)]
        agg = _pessimistic_min(scores)
        if agg is not None:
            df[uid]["LLM_Score"] = agg

    save_json(df, output_path)

    # ── Summary ─────────────────────────────────────────────────────
    n_complete = sum(1 for v in df.values() if len(v.get("LLM_Full_Output", []) or []) >= n_runs)
    print()
    print(f"Saved to:      {output_path}")
    print(f"Rows fully complete ({n_runs} runs each): {n_complete}/{len(df)}")

    # Quick parse-status counts (pseudo-formalisation only sets these)
    if method == "pseudo-formalisation":
        statuses: Dict[str, int] = {}
        for v in df.values():
            for r in v.get("LLM_Full_Output") or []:
                s = (r or {}).get("extraction_status", "?")
                statuses[s] = statuses.get(s, 0) + 1
        if statuses:
            print("Extraction status counts:")
            for k, v in sorted(statuses.items(), key=lambda kv: -kv[1]):
                print(f"  {k:<24} {v}")

    return df


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--method",
        type=str,
        default=DEFAULT_METHOD,
        choices=list(OUTPUT_DIR_BY_METHOD.keys()),
    )
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--model", type=str, default=DEFAULT_MODEL)
    p.add_argument("--effort", type=str, default=DEFAULT_EFFORT)
    p.add_argument("--n-runs", type=int, default=DEFAULT_N_RUNS)
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument(
        "--dev-set-size",
        type=int,
        default=DEFAULT_DEV_SET_SIZE,
        help="Number of problems to sample as the dev set "
        "(0 or >= dataset size = use all).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="Random seed for dev-set sampling.",
    )
    p.add_argument(
        "--unique-id",
        type=str,
        action="append",
        default=None,
        help="Restrict to specific unique_id(s); may be repeated.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        main(
            method=args.method,
            output_dir=args.output_dir,
            model=args.model,
            effort=args.effort,
            n_runs=args.n_runs,
            concurrency=args.concurrency,
            dev_set_size=args.dev_set_size,
            seed=args.seed,
            unique_ids=args.unique_id,
        )
    )
