"""Download the ArxivMathGradingBench PDFs from arXiv.

The released dataset ships only metadata (ids, versions, ground-truth error
locations) — NOT the paper text or PDFs, which remain under the authors'
arXiv licenses. This script fetches each paper's PDF directly from arXiv at
the exact version the benchmark uses, so every download is an ordinary,
author-licensed retrieval from arxiv.org.

For each row it fetches
    https://arxiv.org/pdf/<arxiv_id><version>      (e.g. .../2501.04482v2)
and saves it as
    <out-dir>/arXiv-<arxiv_id><version>.pdf
which is exactly the naming arxiv_grading.py expects (--pdf-dir).

Idempotent (skips files already present) and rate-limited (arXiv asks for
~1 request / 3s). 35 papers => ~2 minutes.

Usage:
    python scripts/download_arxiv_pdfs.py                       # from HF
    python scripts/download_arxiv_pdfs.py --jsonl path/to.jsonl # from local
    python scripts/download_arxiv_pdfs.py --out-dir data/arxiv_grading_bench_pdf_files
"""

import argparse
import json
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HF_REPO = "LukeBailey181Pub/ArxivMathGradingBench"
DEFAULT_OUT = REPO / "data/arxiv_grading_bench_pdf_files"
ARXIV_PDF = "https://arxiv.org/pdf/{id}{ver}"
DELAY_S = 3.0           # arXiv politeness
UA = "ArxivMathGradingBench-downloader/1.0 (mailto:research@example.org)"


def _load_rows(jsonl):
    if jsonl:
        with open(jsonl) as f:
            return [json.loads(l) for l in f if l.strip()]
    from datasets import load_dataset
    return list(load_dataset(HF_REPO)["train"])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", type=str, default=None,
                    help="Local metadata jsonl; if omitted, loads from HF.")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--delay", type=float, default=DELAY_S)
    args = ap.parse_args()

    rows = _load_rows(args.jsonl)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{len(rows)} papers -> {args.out_dir}")

    n_dl = n_skip = n_fail = 0
    for i, r in enumerate(rows, 1):
        aid = r["arxiv_id"]
        ver = r.get("version", "")
        if not ver:
            print(f"  [{i}/{len(rows)}] {aid}: NO version field — skipping "
                  f"(re-export dataset with a version column)")
            n_fail += 1
            continue
        dest = args.out_dir / f"arXiv-{aid}{ver}.pdf"
        if dest.exists() and dest.stat().st_size > 10_000:
            n_skip += 1
            continue
        url = ARXIV_PDF.format(id=aid, ver=ver)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if data[:5] != b"%PDF-":
                raise ValueError("response is not a PDF")
            dest.write_bytes(data)
            n_dl += 1
            print(f"  [{i}/{len(rows)}] {aid}{ver}: {len(data)//1024} KB")
            time.sleep(args.delay)
        except Exception as e:
            n_fail += 1
            print(f"  [{i}/{len(rows)}] {aid}{ver}: FAILED ({type(e).__name__}: {e})")

    print(f"\ndownloaded={n_dl}  skipped(existing)={n_skip}  failed={n_fail}")
    if n_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
