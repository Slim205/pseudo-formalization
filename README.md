# Pseudo-Formalization for Automatic Proof Verification

[![arXiv](https://img.shields.io/badge/arXiv-2605.20531-b31b1b.svg)](https://arxiv.org/abs/2605.20531)
[![Project page](https://img.shields.io/badge/Project-page-1f4e5f.svg)](https://pseudoformalization.github.io)
[![ArxivMathGradingBench](https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-ArxivMathGradingBench-ffd21e.svg)](https://huggingface.co/datasets/LukeBailey181Pub/ArxivMathGradingBench)
[![FirstProofGradingBench](https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-FirstProofGradingBench-ffd21e.svg)](https://huggingface.co/datasets/LukeBailey181Pub/FirstProofGradingBench)


Official implementation for *Pseudo-Formalization for Automatic Proof Verification*.
Three experimental pipelines are
provided:

- **arxiv** — for running our `ArxivMathGradingBench` benchmark
(found at `data/arxiv_grading_bench.jsonl`), research level mathematics.
- **hard2verify** — for running the `Salesforce/Hard2Verify` benchmark, IMO and Putnam level mathematics.
- **firstproof** — for running our `FirstProofGradingBench` benchmark (found at
`data/firstproof_grading_bench.jsonl`), research level mathematics with
expert-refereed errors.

Each pipeline supports two verification methods:

- **baseline** — LLM-as-judge over the full proof (for Hard2Verify, the
step-level judge prompt released by the Hard2Verify authors).
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

- `data/arxiv_grading_bench.jsonl` — the `ArxivMathGradingBench` benchmark,
identical to the
[Hugging Face release](https://huggingface.co/datasets/LukeBailey181Pub/ArxivMathGradingBench).
`benchmark_version` is 1 for the original 35 large dataset and 2 for the 65 added in the
 second version of the paper. The paper PDFs are **not** included 
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
     `decrypt_sample` utility that `decrypt_hard2verify.py` imports, and the
     step-level prompts and verdict parser used by the Hard2Verify baseline.
  2. Run `python scripts/decrypt_hard2verify.py`, which loads
    `Salesforce/Hard2Verify` from Hugging Face, decrypts it, and writes
     `data/hard2verify.json` (and a `.csv` for inspection).
  Note, make sure not to commit the decrypted version of this benchmark in any public 
  repos (as requested by the `Hard2Verify` authors).
- `data/firstproof_grading_bench.jsonl` — the `FirstProofGradingBench` benchmark,
identical to the
[Hugging Face release](https://huggingface.co/datasets/LukeBailey181Pub/FirstProofGradingBench):
16 (problem, submitted proof) pairs with 24 ground-truth errors, each a verbatim
excerpt of the proof (`errored_block`) plus a description.
- `data/firstproof_pfs/<id>.pf.txt` — the Pseudo-Formal rewrite of each
`FirstProofGradingBench` proof used in the paper. The pseudo-formalisation
method verifies these fixed rewrites; every rollout reuses the same one.

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

The faithfulness check audits the whole rewrite against the PDF in a single call
(`--faithfulness whole`, the default and what the paper uses); `--faithfulness
per_component` makes one call per component instead, and `--faithfulness off`
accepts any rewrite that passes the parse check.

The arxiv pipeline expects PDF source files in a directory pointed to by
`--pdf-dir <path>` (you can also use `export ARXIV_PDF_DIR=...`).

### `Hard2Verify`

For running the baseline (the Hard2Verify authors' step-level LLM-as-judge
prompt from `Hard2Verify/utils.py`; each run returns a yes/no verdict per step):

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

### `FirstProofGradingBench`

Both methods use `gpt-5.5` at high reasoning effort for every stage, web search
with GitHub and the First Proof site blocked, and a theorem check before
verification (if it finds that the proof does not prove the problem as posed,
its finding is that rollout's only prediction).

For running the baseline (theorem check, then one referee call over the whole
submission):

```bash
python firstproof_grading.py --method baseline --n-runs 7 \
    --output outputs/firstproof-baseline/grading_full_output.json
```

For running Pseudo-formal + block verification (theorem check on the PF theorem
block, block verification of every block of `data/firstproof_pfs/<id>.pf.txt`,
then the calibrator):

```bash
python firstproof_grading.py --method pseudo-formalisation --n-runs 4 \
    --output outputs/firstproof-pseudo-formalisation/grading_full_output.json
```

Each rollout is then judged by the verifier auto eval (`gpt-5.5`), which records
which ground-truth errors each predicted error matches. Use `--id 03B` (repeatable)
to restrict to specific proofs. Rerunning the same command resumes, running only
missing rollouts.

## Output and metrics

All runners save raw per-run model outputs to a single
`grading_full_output.json`; none of them computes metrics itself. The
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
`LLM_Full_Output` with each run's outputs: per-step predictions in
`step_predictions` (1 = correct, 0 = incorrect) for the baseline, and the
calibrated `step_verification` for pseudo-formalisation.

The metrics are at the step level, with *incorrect step* as the positive
class. Across `k` runs the predictions are aggregated pessimistically — a step
is predicted incorrect if any of the `k` runs flags it — and TP/FP/FN are
tallied over all steps to give step-level precision and recall, again swept
over `k`. To compute them (step and proof level, `k` = 1..n):

```bash
python scripts/hard2verify_metrics.py outputs/hard2verify-pseudo-formalisation/grading_full_output.json
```

Unparsed or missing step verdicts are graded as wrong, as in the Hard2Verify
authors' grading. For `k` < n the script averages over 500 random size-`k`
subsets of runs (as in the paper); `--exact` averages over every subset instead.
Pass `--csv <path>` to save the table.

### `FirstProofGradingBench`

`firstproof_grading.py` writes one `grading_full_output.json` keyed by proof id;
each entry holds the dataset row and a `runs` list with, per rollout, the
theorem check, the verifier outputs, the `predictions` and the judge's
`pred_matches`. To compute precision and recall for each `k`:

```bash
python scripts/firstproof_metrics.py outputs/firstproof-pseudo-formalisation/grading_full_output.json
```

A ground-truth error is a true positive if any prediction from the `k` rollouts
matches it, a prediction matching no ground-truth error is a false positive
(predictions are not deduplicated), and TP/FP/FN are summed over proofs. Each
`k` is the exact mean over all subsets of `k` rollouts. For
pseudo-formalisation the script reports `pf_calibrated` (the paper's method:
only block flags the calibrator kept) and `pf_uncalibrated` (every block flag).
Pass `--csv <path>` to save the table.

## Project structure

```
arxiv_grading.py         # arxiv pipeline entry point
hard2verify_grading.py   # Hard2Verify pipeline entry point
firstproof_grading.py    # FirstProofGradingBench pipeline entry point
src/
  utils.py               # Data loading, metrics, plotting utilities
  verifier/
    verifier.py          # Verifierbaseline and PseudoFormalisationVerifier
    complex_verifier.py  # Decomposed-rewriter (IMO/H2V)
    arxiv_complex_verifier.py
    prompts.py           # Prompt templates
    complex_prompts.py
    arxiv_complex_prompts.py
    firstproof_verifier.py   # FirstProofBaseline, FirstProofPFVerifier, verifier auto eval
    firstproof_prompts.py
data/
  arxiv_grading_bench.jsonl
  firstproof_grading_bench.jsonl
  firstproof_pfs/        # Pseudo-Formal rewrites of the FirstProofGradingBench proofs
scripts/                 # metrics, PDF download, and one-off utility scripts
Hard2Verify/             # NOT included — clone the Hard2Verify repo here for its decrypt utility and baseline prompts (see Data)
```

