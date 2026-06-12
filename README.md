# Pseudo-Formalization for Automatic Proof Verification

[![arXiv](https://img.shields.io/badge/arXiv-2605.20531-b31b1b.svg)](https://arxiv.org/abs/2605.20531)


Official implementation for *Pseudo-Formalization for Automatic Proof Verification*.
Two experimental pipelines are
provided:

- **arxiv** — for running our `ArxivMathGradingBench` benchmark
(found at `data/arxiv_grading_bench.jsonl`), research level mathematics.
- **hard2verify** — for running the `Salesforce/Hard2Verify` benchmark, IMO and Putnam level mathematics.

Both pipelines support two verification methods:

- **baseline** — LLM-as-judge over the full proof.
- **pseudo-formalisation** — translates the proof into Pseudo-Formal (PF)
modules, then runs Block Verification (BV) on each module independently.

## Setup

### Requirements

- Python 3.10+
- `openai`, `pandas`, `tqdm`, `matplotlib`, `seaborn`, `scikit-learn`,
`datasets`

### Environment variables

```bash
export OPENAI_API_KEY="your-openai-api-key"
```

### Data

- `data/arxiv_grading_bench.jsonl` — the `ArxivMathGradingBench` benchmark
metadata (arXiv IDs, versions, ground-truth error locations). The paper PDFs are **not** included 
in this repo, and need to be downloaded by running:
  ```bash
  python scripts/download_arxiv_pdfs.py --out-dir data/arxiv_grading_bench_pdf_files
  ```
  This fetches each paper from arXiv at the benchmark version into
  `arXiv-<id><version>.pdf`. Then point the runner at that directory with
  `--pdf-dir`.
- `data/hard2verify.json` — the decrypted Hard2Verify data read by
`hard2verify_grading.py`. **Not included**; to produce it:
  1. Clone the Hard2Verify repo into `./Hard2Verify/` from
     https://github.com/SalesforceAIResearch/Hard2Verify — it provides the
     `decrypt_sample` utility that `decrypt_hard2verify.py` imports.
  2. Run `python scripts/decrypt_hard2verify.py`, which loads
    `Salesforce/Hard2Verify` from Hugging Face, decrypts it, and writes
     `data/hard2verify.json` (and a `.csv` for inspection).
  Note, make sure not to commit the decrypted version of this benchmark in any public 
  repos (as requested by the `Hard2Verify` authors).

## Usage

### `ArxivMathGradingBench`

First download the paper PDFs (see Data above):

```bash
python scripts/download_arxiv_pdfs.py --out-dir data/arxiv_grading_bench_pdf_files
```

For running the baseline:

```bash
python arxiv_grading.py --method baseline \
    --model gpt-5.4-mini-2026-03-17 --effort medium \
    --n-runs 1 --concurrency 16 \
    --pdf-dir data/arxiv_grading_bench_pdf_files
```

For running Pseudo-formal + block verification:

```bash
python arxiv_grading.py --method pseudo-formalisation \
    --model gpt-5.4-mini-2026-03-17 --effort medium \
    --n-runs 1 --concurrency 16 \
    --pdf-dir data/arxiv_grading_bench_pdf_files
```

The arxiv pipeline expects PDF source files in a directory pointed to by
`--pdf-dir <path>` (you can also use `export ARXIV_PDF_DIR=...`).

### `Hard2Verify`

For running the baseline:

```bash
python hard2verify_grading.py --method baseline \
    --model gpt-5.4-mini-2026-03-17 --effort medium \
    --n-runs 1 --dev-set-size 200 --concurrency 16 \
    --output-dir outputs/hard2verify-baseline
```

For running Pseudo-formal + block verification:

```bash
python hard2verify_grading.py --method pseudo-formalisation \
    --model gpt-5.4-mini-2026-03-17 --effort medium \
    --n-runs 1 --dev-set-size 200 --concurrency 16 \
    --output-dir outputs/hard2verify-pseudo-formalisation
```

For the pseudo-formalisation method, step-level calibration runs inside this
command: it uses the original solution steps from `data/hard2verify.json`,
audits the rewritten-proof verifier flags, and emits per-step verdicts plus 
incorrect steps in each run's `step_verification` field.

## Output and metrics

Both runners save raw per-run model outputs to a single
`grading_full_output.json`; neither runner computes metrics itself. The
metrics we report (and how the saved output maps to them) are defined per
pipeline below.

### `ArxivMathGradingBench`

`arxiv_grading.py` writes `outputs/arxiv-<method>/grading_full_output.json`.
Per paper it stores the ground-truth error location and, for each of the `n`
runs, the model's raw output: `extracted_errors` (a list of
`{location, description}`), `extracted_locations`, an `extraction_status`, and
token `usage`.

The metrics treat *finding an error* as the positive class. For each predicted
error, an LLM judge decides whether its location refers to a ground-truth
error location of that paper (the same labelled result — e.g. `Theorem 19` —
or a step inside its proof). To aggregate `k` parallel runs, the predicted
errors from the `k` runs are unioned and de-duplicated by location. Counting
across all papers gives

- **TP** = ground-truth locations matched by some prediction,
- **FP** = predictions matching no ground-truth location,
- **FN** = ground-truth locations matched by no prediction,

so `precision = TP / (TP + FP)` and `recall = TP / (TP + FN)`. Sweeping `k`
from `1..n` traces a precision–recall curve. Because the benchmark only
annotates the author-disclosed error(s), FP is an upper bound and precision a
lower bound on the true values.

Compute these from a run's output with:

```bash
python scripts/arxiv_metrics.py outputs/arxiv-baseline/grading_full_output.json
```

This runs the LLM judge (`gpt-5.4-mini` by default) and prints
precision/recall for each `k`. Pass `--csv <path>` to save the table.

### `Hard2Verify`

`hard2verify_grading.py` writes `<output-dir>/grading_full_output.json`
(default `outputs/hard2verify-<method>/`). Per problem it stores the human
step labels (`human_labels`), a binary `Points` (7 iff all steps correct), and
`LLM_Full_Output` with each run's per-step and per-solution predictions (plus
the calibrated `step_verification` for pseudo-formalisation).

The metrics are at the step level, with *incorrect step* as the positive
class. Across `k` runs the predictions are aggregated pessimistically — a step
is predicted incorrect if any of the `k` runs flags it — and TP/FP/FN are
tallied over all steps to give step-level precision and recall, again swept
over `k`.

## Project structure

```
arxiv_grading.py         # arxiv pipeline entry point
hard2verify_grading.py   # Hard2Verify pipeline entry point
src/
  utils.py               # Data loading, metrics, plotting utilities
  verifier/
    verifier.py          # Verifierbaseline and PseudoFormalisationVerifier
    complex_verifier.py  # Decomposed-rewriter (IMO/H2V)
    arxiv_complex_verifier.py
    prompts.py           # Prompt templates
    complex_prompts.py
    arxiv_complex_prompts.py
data/
  arxiv_grading_bench.jsonl
scripts/                 # metrics, PDF download, and one-off utility scripts
Hard2Verify/             # NOT included — clone the Hard2Verify repo here for its decrypt utility (see Data)
```

