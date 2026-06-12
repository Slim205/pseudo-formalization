"""Decrypt the Salesforce/Hard2Verify dataset using utils.decrypt_sample
from the ../hard2verify repo, and save to ./data/hard2verify.csv (and a
parallel JSON file) for inspection.
"""

import json
import csv
import sys
from pathlib import Path

# Make ../hard2verify importable so we can use its utils.decrypt_sample
HARD2VERIFY_DIR = Path(__file__).resolve().parents[1] / "Hard2Verify"
sys.path.insert(0, str(HARD2VERIFY_DIR))

from utils import decrypt_sample  # noqa: E402

from datasets import load_dataset  # noqa: E402


OUT_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH = OUT_DIR / "hard2verify.csv"
JSON_PATH = OUT_DIR / "hard2verify.json"


def _to_cell(v):
    """Render a Python value into a CSV-friendly string.

    Lists/dicts are JSON-serialized so CSV cells stay intact while
    preserving structure.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def main():
    print("Loading Salesforce/Hard2Verify (test split)...")
    ds = load_dataset("Salesforce/Hard2Verify", split="test")
    print(f"  loaded: {ds.num_rows} rows, columns: {ds.column_names}")

    print("Decrypting...")
    ds = ds.map(decrypt_sample)
    print("  done.")

    # Save full structured form to JSON
    rows = [dict(r) for r in ds]
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"Wrote JSON: {JSON_PATH}  ({JSON_PATH.stat().st_size:,} bytes)")

    # Save CSV (lists/dicts JSON-encoded inside cells)
    cols = list(ds.column_names)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([_to_cell(r.get(c)) for c in cols])
    print(f"Wrote CSV:  {CSV_PATH}  ({CSV_PATH.stat().st_size:,} bytes)")

    # Quick sanity print of the first decrypted row
    print()
    print("=== first row preview ===")
    first = rows[0]
    for k in cols:
        v = first.get(k)
        if isinstance(v, str):
            preview = v if len(v) <= 200 else v[:197] + "..."
            print(f"  {k} (str, {len(v)} chars): {preview!r}")
        else:
            preview = json.dumps(v, ensure_ascii=False)
            preview = preview if len(preview) <= 200 else preview[:197] + "..."
            print(f"  {k} ({type(v).__name__}): {preview}")


if __name__ == "__main__":
    main()
