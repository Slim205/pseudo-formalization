"""Run a FirstProofGradingBench verifier and judge its output.

Two verification methods are available, selected by ``--method``:
- ``baseline``: ``FirstProofBaseline`` (theorem check, then one web-search
  LLM-as-judge referee call over the whole submission).
- ``pseudo-formalisation``: ``FirstProofPFVerifier`` (theorem check on the PF
  theorem block, then web-search block verification of every block of the
  proof's fixed Pseudo-Formal rewrite from ``data/firstproof_pfs/``, then the
  calibrator).

Every rollout is judged by the verifier auto eval (``judge_rollout``), which
matches that rollout's predicted errors to the ground-truth errors. Metrics are
computed separately by ``scripts/firstproof_metrics.py``.

Output: one ``grading_full_output.json`` keyed by proof id, each entry holding
the dataset row and a ``runs`` list with one record per rollout. Resumes from an
existing output file, running only the missing rollouts.
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

load_dotenv()

from tqdm import tqdm

from src.verifier.firstproof_verifier import FirstProofBaseline, FirstProofPFVerifier
from src.utils import save_json, read_json


REPO_ROOT = Path(__file__).resolve().parent
DATASET_PATH = REPO_ROOT / "data" / "firstproof_grading_bench.jsonl"
PF_DIR = REPO_ROOT / "data" / "firstproof_pfs"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_EFFORT = "high"
DEFAULT_N_RUNS = 4
DEFAULT_CONCURRENCY = 64
DEFAULT_METHOD = "pseudo-formalisation"

OUTPUT_PATHS_BY_METHOD = {
    "baseline": REPO_ROOT / "outputs" / "firstproof-baseline" / "grading_full_output.json",
    "pseudo-formalisation": REPO_ROOT / "outputs" / "firstproof-pseudo-formalisation"
    / "grading_full_output.json",
}


def _load_rows(method: str, ids: List[str] = None) -> List[Dict[str, Any]]:
    rows = [json.loads(l) for l in DATASET_PATH.read_text().splitlines() if l.strip()]
    if ids:
        rows = [r for r in rows if r["id"] in set(ids)]
    if method == "pseudo-formalisation":
        for r in rows:
            pf = PF_DIR / f"{r['id']}.pf.txt"
            if not pf.exists():
                raise FileNotFoundError(f"missing Pseudo-Formal proof {pf}")
            r["pf_text"] = pf.read_text()
    return rows


async def main(
    output_path: Path,
    method: str = DEFAULT_METHOD,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    n_runs: int = DEFAULT_N_RUNS,
    concurrency: int = DEFAULT_CONCURRENCY,
    ids: List[str] = None,
) -> Dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = _load_rows(method, ids)
    print(f"Loaded {len(rows)} proofs from {DATASET_PATH}")

    cls = FirstProofPFVerifier if method == "pseudo-formalisation" else FirstProofBaseline
    verifier = cls(n=n_runs, model=model, effort=effort, concurrency=concurrency)

    state: Dict[str, Any] = read_json(output_path) if output_path.exists() else {}
    for r in rows:
        entry = state.setdefault(r["id"], {"runs": []})
        entry.update({k: v for k, v in r.items() if k != "pf_text"})

    async def one(row: Dict[str, Any], run_idx: int):
        try:
            result = await verifier.process_row(row)
            result["judge"] = await verifier.judge_rollout(row, result["predictions"])
        except Exception as e:  # keep the other rollouts going; rerun to retry
            return row["id"], run_idx, e
        result["run_idx"] = run_idx
        return row["id"], run_idx, result

    tasks = []
    for r in rows:
        done = {x["run_idx"] for x in state[r["id"]]["runs"]}
        for i in range(n_runs):
            if i not in done:
                tasks.append(asyncio.create_task(one(r, i)))
    print(f"Running {len(tasks)} rollouts ({method}, n_runs={n_runs}, "
          f"model={model}, effort={effort})")

    failed = []
    for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), unit="rollout"):
        sid, run_idx, result = await coro
        if isinstance(result, Exception):
            failed.append((sid, run_idx, result))
            continue
        state[sid]["runs"].append(result)
        state[sid]["runs"].sort(key=lambda x: x["run_idx"])
        save_json(state, output_path)

    save_json(state, output_path)
    print(f"Saved {output_path}")
    if failed:
        for sid, run_idx, e in failed:
            print(f"  FAILED {sid} rollout {run_idx}: {type(e).__name__}: {e}")
        raise SystemExit(f"{len(failed)} rollouts failed; rerun the same command to retry them")
    return state


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--method", type=str, default=DEFAULT_METHOD,
                   choices=list(OUTPUT_PATHS_BY_METHOD.keys()))
    p.add_argument("--output", type=Path, default=None,
                   help="Output JSON path. If omitted, defaults per method.")
    p.add_argument("--model", type=str, default=DEFAULT_MODEL)
    p.add_argument("--effort", type=str, default=DEFAULT_EFFORT)
    p.add_argument("--n-runs", type=int, default=DEFAULT_N_RUNS)
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--id", type=str, action="append", default=None,
                   help="Restrict to a proof id, e.g. 03B (may be repeated).")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(
        output_path=args.output or OUTPUT_PATHS_BY_METHOD[args.method],
        method=args.method,
        model=args.model,
        effort=args.effort,
        n_runs=args.n_runs,
        concurrency=args.concurrency,
        ids=args.id,
    ))
