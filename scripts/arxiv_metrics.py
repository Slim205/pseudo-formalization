r"""Compute per-error precision/recall for an arxiv_grading.py output.

Reads a `grading_full_output.json` produced by `arxiv_grading.py`, matches
each predicted error against the paper's ground-truth error location(s) with
an LLM judge (the paper is attached as a PDF so the judge can resolve where a
predicted location sits in the paper's structure), and reports precision and
recall swept over k = 1..n parallel runs.

Metric (positive class = "finding the error"), summed over papers, with
ground-truth locations Y and predicted locations Ŷ per paper:

    TP = Σ |Ŷ ∩ Y|     FP = Σ |Ŷ \ Y|     FN = Σ |Y \ Ŷ|
    precision = TP / (TP + FP)            recall = TP / (TP + FN)

For each k the predicted errors of the k sampled runs are unioned and
de-duplicated by location. For k < n the result is averaged over `--boot`
random size-k subsets per paper; k = n is deterministic.

Judge verdicts are cached on disk (keyed by paper + prediction), so re-runs
and the bootstrap make no extra API calls. The PDF path is read from each
paper's `pdf_path` field in the output JSON (recorded by arxiv_grading.py).

Usage:
    python scripts/arxiv_metrics.py outputs/arxiv-baseline/grading_full_output.json
    python scripts/arxiv_metrics.py <out.json> --csv metrics.csv --judge-model gpt-5.4-mini-2026-03-17
"""

import argparse
import asyncio
import base64
import csv
import hashlib
import json
import os
import random
import re
import unicodedata
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv()

DEFAULT_JUDGE_MODEL = "gpt-5.4-mini-2026-03-17"
JUDGE_CONCURRENCY = 16
B = 500
SEED = 42

JUDGE_PROMPT = """You must decide whether a verifier's predicted error refers to a known ground-truth (GT) error location in a mathematics paper. The full paper is attached as a PDF — use it to resolve where the predicted location actually sits in the paper's structure.

Paper title: {title}

Ground-truth error locations (the author corrected errors at these locations):
{gt_list}

Predicted error:
- Location: {pred_loc}
- Description: {pred_desc}

A predicted error matches a GT location ONLY if one of these holds:
(a) its location names the same result — same number/letter label (e.g. "proof of Theorem 19" or "Theorem 19, equation (4.2)" match GT "Theorem 19"); or
(b) in the attached paper, its location is a specific step, claim, case, equation, or passage that lies INSIDE the GT result's statement or proof (e.g. "Claim 2.2 in the proof of Theorem 2.1" matches GT "Theorem 2.1").

It is NOT a match if the predicted location names a DIFFERENT numbered result or a different part of the paper that merely uses, cites, depends on, or inherits a flaw from the GT result — even if the description discusses the GT result extensively. Different numbers never match ("Theorem 18" does not match GT "Theorem 19").

Answer with ONLY a JSON object, no other text:
{{"matched_gt_indices": [<0-based indices into the GT list, empty if none>]}}"""


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).lower()
    return re.sub(r"[^a-z0-9.]+", " ", s).strip()


def _gt_items(gt: str):
    """Split a GT field like 'Theorem 3.4, Corollary 3.5' into items."""
    return [p.strip() for p in re.split(r"[,;]| and ", gt or "") if p.strip()]


def _load(path: Path):
    """Per paper: arxiv_id, title, gts, pdf_path/filename, and per_run lists
    of {loc, desc} predictions. Returns (papers, n) where n is the common
    (minimum) run count; papers with fewer than n runs are dropped."""
    d = json.loads(path.read_text())
    papers = []
    counts = []
    for v in d.values():
        runs = sorted(v.get("runs") or [], key=lambda r: r.get("run_idx", 0))
        if not runs:
            continue
        counts.append(len(runs))
        papers.append({
            "arxiv_id": v.get("arxiv_id") or "",
            "title": v.get("title") or "",
            "gts": _gt_items(v.get("ground_truth_location") or ""),
            "pdf_path": v.get("pdf_path") or "",
            "pdf_filename": v.get("pdf_filename") or "",
            "runs": [
                [
                    {"loc": (e.get("location") or "").strip(),
                     "desc": (e.get("description") or "").strip()}
                    for e in (r.get("extracted_errors") or [])
                ]
                for r in runs
            ],
        })
    if not counts:
        raise SystemExit("No papers with runs found in the output file.")
    n = min(counts)
    papers = [p for p in papers if len(p["runs"]) >= n]
    for p in papers:
        p["runs"] = p["runs"][:n]
    return papers, n


# ── LLM judge (PDF-attached, disk-cached) ─────────────────────────────


def _judge_key(paper, pred):
    raw = json.dumps([paper["arxiv_id"], paper["gts"], pred["loc"], pred["desc"]],
                     ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()


def _parse_verdict(text, n_gts):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        idxs = json.loads(m.group(0)).get("matched_gt_indices", [])
        return sorted({i for i in idxs if isinstance(i, int) and 0 <= i < n_gts})
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


async def _judge_all(papers, model, cache_path):
    from openai import AsyncOpenAI

    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    todo = {}
    for paper in papers:
        if not paper["gts"]:
            continue
        for run in paper["runs"]:
            for pred in run:
                k = _judge_key(paper, pred)
                # Judge if unseen OR previously cached as a failure (dict);
                # successful verdicts are lists and are reused as-is.
                if k not in todo and not isinstance(cache.get(k), list):
                    todo[k] = (paper, pred)

    print(f"  judge: {len(todo)} unique predictions to judge ({len(cache)} cached)")
    if not todo:
        return cache

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    sem = asyncio.Semaphore(JUDGE_CONCURRENCY)
    pdf_cache = {}

    def _save():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))

    async def _one(key, paper, pred):
        if paper["pdf_path"] not in pdf_cache:
            pdf_cache[paper["pdf_path"]] = base64.b64encode(
                Path(paper["pdf_path"]).read_bytes()).decode()
        prompt = JUDGE_PROMPT.format(
            title=paper["title"],
            gt_list="\n".join(f"{i}. {g}" for i, g in enumerate(paper["gts"])),
            pred_loc=pred["loc"] or "(none given)",
            pred_desc=pred["desc"] or "(none given)",
        )
        content = [
            {"type": "input_file", "filename": paper["pdf_filename"],
             "file_data": f"data:application/pdf;base64,{pdf_cache[paper['pdf_path']]}"},
            {"type": "input_text", "text": prompt},
        ]
        last_err = "no successful response"
        async with sem:
            for attempt in range(6):
                try:
                    resp = await client.responses.create(
                        model=model,
                        input=[{"role": "user", "content": content}],
                        text={"format": {"type": "text"}},
                        max_output_tokens=5000,
                        reasoning={"effort": "low"},
                    )
                    v = _parse_verdict(getattr(resp, "output_text", "") or "",
                                       len(paper["gts"]))
                    if v is not None:  # parsed (incl. a genuine empty match)
                        return key, v
                    last_err = "unparseable judge response"
                except Exception as e:
                    last_err = repr(e)
                    await asyncio.sleep(5 * (2 ** attempt))
        # Persistent failure: cache a FAILED marker (a dict, distinct from a
        # real list verdict) — NOT an empty `[]` "no match". It is recorded so
        # it can be audited, retried on re-run, and never silently counted as
        # a true negative.
        return key, {"failed": True, "error": last_err}

    done = 0
    fail_examples = []
    for fut in asyncio.as_completed([_one(k, p, e) for k, (p, e) in todo.items()]):
        key, v = await fut
        cache[key] = v
        if isinstance(v, dict) and len(fail_examples) < 3:
            fail_examples.append(v.get("error"))
        done += 1
        if done % 100 == 0:
            print(f"    judged {done}/{len(todo)}")
            _save()
    _save()
    n_failed = sum(1 for v in cache.values() if isinstance(v, dict))
    if n_failed:
        print("\n" + "!" * 70)
        print(f"!! WARNING: {n_failed} prediction(s) FAILED to judge after retries.")
        print("!! They are cached as 'failed' (NOT as a match), and re-running")
        print("!! this script will retry them. Until they succeed, the reported")
        print("!! precision/recall are PROVISIONAL (failed preds count as no-match).")
        print(f"!! Example errors: {fail_examples}")
        print("!" * 70 + "\n")
    return cache


# ── Metrics ───────────────────────────────────────────────────────────


def _precompute(papers, verdicts):
    for paper in papers:
        for run in paper["runs"]:
            for pred in run:
                pred["key"] = _norm(pred["loc"])
                mv = verdicts.get(_judge_key(paper, pred))
                # Only a list is a real verdict; a failure marker (dict) or a
                # missing entry counts as no match (provisional; warned above).
                pred["matched"] = frozenset(mv) if isinstance(mv, list) else frozenset()


def _tp_fp_fn(papers, idxs_per):
    tp = fp = fn = 0
    for paper, idxs in zip(papers, idxs_per):
        seen, union = set(), []
        for i in idxs:
            for pred in paper["runs"][i]:
                if pred["key"] in seen:
                    continue
                seen.add(pred["key"])
                union.append(pred)
        hit = set()
        for pred in union:
            hit |= pred["matched"]
            if not pred["matched"]:
                fp += 1
        tp += len(hit)
        fn += len(paper["gts"]) - len(hit)
    return tp, fp, fn


def _boot(papers, k, n, rng, boot):
    if k >= n:
        tp, fp, fn = _tp_fp_fn(papers, [list(range(n))] * len(papers))
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        return p, r
    ps, rs = [], []
    allidx = list(range(n))
    for _ in range(boot):
        idxs = [rng.sample(allidx, k) for _ in papers]
        tp, fp, fn = _tp_fp_fn(papers, idxs)
        ps.append(tp / (tp + fp) if tp + fp else 0.0)
        rs.append(tp / (tp + fn) if tp + fn else 0.0)
    return float(np.mean(ps)), float(np.mean(rs))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output_json", type=Path,
                    help="grading_full_output.json from arxiv_grading.py")
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    ap.add_argument("--cache", type=Path, default=None,
                    help="Judge verdict cache (default: alongside output_json).")
    ap.add_argument("--csv", type=Path, default=None, help="Optional CSV of per-k metrics.")
    ap.add_argument("--boot", type=int, default=B, help="Bootstrap iterations for k<n.")
    args = ap.parse_args()

    papers, n = _load(args.output_json)
    print(f"{len(papers)} papers, n={n} runs each (judge: {args.judge_model})")
    cache_path = args.cache or args.output_json.with_name("arxiv_judge_cache.json")
    verdicts = asyncio.run(_judge_all(papers, args.judge_model, cache_path))
    _precompute(papers, verdicts)

    rng = random.Random(SEED)
    rows = []
    print(f"\n{'k':>3}  {'precision':>10}  {'recall':>8}")
    for k in range(1, n + 1):
        p, r = _boot(papers, k, n, rng, args.boot)
        rows.append({"k": k, "precision": p, "recall": r})
        print(f"{k:>3}  {100*p:>9.1f}%  {100*r:>7.1f}%")

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["k", "precision", "recall"])
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {args.csv}")


if __name__ == "__main__":
    main()
