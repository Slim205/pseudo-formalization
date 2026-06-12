"""Run an arxiv verifier over the local arxiv_grading_bench.jsonl dataset
and save full per-run results to JSON.

Two verification methods are available, selected by ``--method``:
- ``baseline`` (default): one model call per paper using ``ArxivVerifierBaseline``.
- ``pseudo-formalisation``: full pseudo-formalisation pipeline per paper using
  ``ArxivDecomposedVerifier`` (rewrite + faithfulness retries + per-component
  verification + meta-verification).

Both methods send ONLY the rendered PDF to the model (PDF-only input mode —
no raw LaTeX source) and produce a list of errors with locations keyed by the
rendered PDF labels (e.g. "Theorem 19", "Lemma 4.3"). Ground truth from the
dataset is preserved alongside in the output JSON.
"""

import argparse
import asyncio
import base64
import glob
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

load_dotenv()

from datasets import Dataset
from tqdm import tqdm

from src.verifier.verifier import (
    ArxivVerifierBaseline,
    ArxivDecomposedVerifier,
)
from src.verifier.arxiv_complex_verifier import (
    ArxivComplexPseudoFormalisationVerifier,
)
from src.utils import save_json, read_json


DATASET_PATH = Path(__file__).resolve().parent / "data" / "arxiv_grading_bench.jsonl"
DEFAULT_PDF_DIR = Path(os.environ.get("ARXIV_PDF_DIR", "data/arxiv_pdfs"))
DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
DEFAULT_EFFORT = "medium"
DEFAULT_N_RUNS = 4
DEFAULT_CONCURRENCY = 16
DEFAULT_METHOD = "baseline"

OUTPUT_PATHS_BY_METHOD = {
    "baseline": Path("./outputs/arxiv-baseline/grading_full_output.json"),
    "pseudo-formalisation": Path(
        "./outputs/arxiv-pseudo-formalisation/grading_full_output.json"
    ),
    "arxiv-complex-pseudo-formalisation": Path(
        "./outputs/arxiv-complex-pseudo-formalisation/grading_full_output.json"
    ),
}
DEFAULT_OUTPUT = OUTPUT_PATHS_BY_METHOD[DEFAULT_METHOD]


def _resolve_pdf_path(pdf_dir: Path, arxiv_id: str) -> Path:
    """Find the PDF for *arxiv_id* under *pdf_dir*. Loud-error if missing."""
    pattern = str(pdf_dir / f"arXiv-{arxiv_id}*.pdf")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(
            f"\n\n!!! PDF NOT FOUND for arxiv_id={arxiv_id!r} !!!\n"
            f"    Searched: {pattern}\n"
            f"    No file matched. Check that the PDF is present in {pdf_dir} "
            f"with the expected naming convention (arXiv-<id>v<N>.pdf).\n"
        )
    if len(matches) > 1:
        print(
            f"!! WARNING: multiple PDFs match arxiv_id={arxiv_id!r}: "
            f"{[os.path.basename(m) for m in matches]} — picking the last (highest version)."
        )
    return Path(matches[-1])


def _load_pdf_b64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _row_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "arxiv_id": row["arxiv_id"],
        "url": row.get("url"),
        "category": row.get("category"),
        "category_name_readable": row.get("category_name_readable"),
        "title": row.get("title_extracted_from_tex"),
        "tex_number_of_lines": row.get("tex_number_of_lines"),
        "ground_truth_location": row.get("Location of Error"),
        "comments": row.get("comments"),
    }


async def _run_one(
    verifier: Any,
    sem: asyncio.Semaphore,
    arxiv_id: str,
    run_idx: int,
    row_for_verifier: Dict[str, Any],
) -> Dict[str, Any]:
    async with sem:
        result = await verifier.process_row(row_for_verifier)
    result["run_idx"] = run_idx
    result["arxiv_id"] = arxiv_id
    return result


def _build_verifier(method: str, model: str, effort: str) -> Any:
    if method == "baseline":
        return ArxivVerifierBaseline(model=model, effort=effort)
    if method == "pseudo-formalisation":
        return ArxivDecomposedVerifier(model=model, effort=effort)
    if method == "arxiv-complex-pseudo-formalisation":
        # Defaults: faithfulness on, meta-verify on, n_verifications=1,
        # global block check off (friend can flip on later if useful).
        return ArxivComplexPseudoFormalisationVerifier(
            model=model,
            effort=effort,
            n_verifications=1,
            faithfulness_check=True,
            meta_verify=True,
            block_verifier=True,
            global_block_check=False,
            max_rewrite_retries=5,
        )
    raise ValueError(
        f"Unknown method {method!r}. Expected one of: "
        f"{list(OUTPUT_PATHS_BY_METHOD.keys())}"
    )


async def main(
    output_path: Path = DEFAULT_OUTPUT,
    pdf_dir: Path = DEFAULT_PDF_DIR,
    method: str = DEFAULT_METHOD,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    n_runs: int = DEFAULT_N_RUNS,
    concurrency: int = DEFAULT_CONCURRENCY,
    limit: int = -1,
    arxiv_ids: list = None,
    save_every: int = 10,
) -> Dict[str, Any]:

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not pdf_dir.is_dir():
        raise FileNotFoundError(
            f"\n\n!!! PDF DIRECTORY NOT FOUND: {pdf_dir}\n"
            f"    Pass --pdf-dir to point to the directory containing "
            f"arXiv-<id>v<N>.pdf files.\n"
        )

    print(f"Loading dataset: {DATASET_PATH}")
    ds = Dataset.from_json(str(DATASET_PATH))
    if arxiv_ids:
        wanted = set(arxiv_ids)
        keep_idx = [i for i, r in enumerate(ds) if r["arxiv_id"] in wanted]
        if not keep_idx:
            raise ValueError(
                f"No dataset rows matched the requested arxiv_ids: {arxiv_ids!r}"
            )
        ds = ds.select(keep_idx)
    if limit > 0:
        ds = ds.select(range(min(limit, ds.num_rows)))
    print(f"  {ds.num_rows} papers")

    # Pre-resolve and load all PDFs up front so any missing file fails the
    # whole run loudly before we send a single API call.
    print(f"Resolving PDFs from: {pdf_dir}")
    pdf_info: Dict[str, Dict[str, Any]] = {}
    for row in ds:
        aid = row["arxiv_id"]
        pdf_path = _resolve_pdf_path(pdf_dir, aid)
        pdf_b64 = _load_pdf_b64(pdf_path)
        pdf_info[aid] = {
            "path": str(pdf_path),
            "filename": pdf_path.name,
            "b64": pdf_b64,
            "size_bytes": os.path.getsize(pdf_path),
        }
    print(f"  resolved {len(pdf_info)} PDFs")

    # Build / resume state
    state: Dict[str, Any] = {}
    if output_path.exists():
        print(f"Resuming from existing output: {output_path}")
        state = read_json(output_path)

    for row in ds:
        aid = row["arxiv_id"]
        if aid not in state:
            state[aid] = {**_row_metadata(row), "runs": []}
        else:
            # Refresh metadata in case dataset updated; keep prior runs.
            meta = _row_metadata(row)
            for k, v in meta.items():
                state[aid][k] = v
            state[aid].setdefault("runs", [])
        # Always record the pdf path/filename used for this run.
        state[aid]["pdf_path"] = pdf_info[aid]["path"]
        state[aid]["pdf_filename"] = pdf_info[aid]["filename"]
        state[aid]["pdf_size_bytes"] = pdf_info[aid]["size_bytes"]

    verifier = _build_verifier(method, model, effort)
    sem = asyncio.Semaphore(concurrency)

    # Build the task list: only fire calls that haven't been completed yet.
    tasks: List[asyncio.Task] = []
    aid_to_row: Dict[str, Dict[str, Any]] = {row["arxiv_id"]: row for row in ds}

    for aid, row in aid_to_row.items():
        existing = len(state[aid]["runs"])
        for run_idx in range(existing, n_runs):
            tasks.append(
                asyncio.create_task(
                    _run_one(
                        verifier,
                        sem,
                        aid,
                        run_idx,
                        {
                            "pdf_b64": pdf_info[aid]["b64"],
                            "pdf_filename": pdf_info[aid]["filename"],
                        },
                    )
                )
            )

    print(
        f"Dispatching {len(tasks)} runs "
        f"({ds.num_rows} papers x {n_runs} runs, method={method}, "
        f"concurrency={concurrency}, model={model}, effort={effort})"
    )
    if not tasks:
        print("Nothing to do — output already complete.")
        save_json(state, output_path)
        return state

    started = time.perf_counter()
    completed = 0
    for coro in tqdm(
        asyncio.as_completed(tasks),
        total=len(tasks),
        desc="Refereeing",
        unit="run",
    ):
        result = await coro
        aid = result["arxiv_id"]
        state[aid]["runs"].append(result)
        # Keep runs ordered by run_idx so the file is easy to read.
        state[aid]["runs"].sort(key=lambda r: r["run_idx"])

        completed += 1
        if save_every > 0 and completed % save_every == 0:
            save_json(state, output_path)

    save_json(state, output_path)
    elapsed = time.perf_counter() - started
    print(f"Done. {completed} runs in {elapsed:.1f}s. Saved to {output_path}")

    # Quick parse-status summary.
    status_counts: Dict[str, int] = {}
    for entry in state.values():
        for r in entry["runs"]:
            s = r.get("extraction_status", "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1
    print("Extraction status counts:")
    for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<24} {v}")

    return state


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--method",
        type=str,
        default=DEFAULT_METHOD,
        choices=list(OUTPUT_PATHS_BY_METHOD.keys()),
        help="Verification method.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path. If omitted, defaults per method "
        "(baseline -> outputs/arxiv-baseline/..., pseudo-formalisation -> "
        "outputs/arxiv-pseudo-formalisation/...).",
    )
    p.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR,
                   help="Directory containing arXiv-<id>v<N>.pdf files.")
    p.add_argument("--model", type=str, default=DEFAULT_MODEL)
    p.add_argument("--effort", type=str, default=DEFAULT_EFFORT)
    p.add_argument("--n-runs", type=int, default=DEFAULT_N_RUNS)
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--limit", type=int, default=-1,
                   help="If > 0, only process this many papers.")
    p.add_argument(
        "--arxiv-id",
        type=str,
        action="append",
        default=None,
        help="Restrict to a specific arxiv_id (may be repeated to allow several).",
    )
    p.add_argument("--save-every", type=int, default=10,
                   help="Checkpoint frequency in completed runs.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    output_path = args.output or OUTPUT_PATHS_BY_METHOD[args.method]
    asyncio.run(
        main(
            output_path=output_path,
            pdf_dir=args.pdf_dir,
            method=args.method,
            model=args.model,
            effort=args.effort,
            n_runs=args.n_runs,
            concurrency=args.concurrency,
            limit=args.limit,
            arxiv_ids=args.arxiv_id,
            save_every=args.save_every,
        )
    )
