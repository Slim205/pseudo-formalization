import asyncio
from tqdm import tqdm
from src.utils import (
    parse_score,
    save_json,
)


class pipeline:

    def __init__(self, concurrency, method="baseline"):
        self.concurrency = concurrency
        self.method = method

    async def run(self, verifier, df, output):
        sem = asyncio.Semaphore(self.concurrency)

        async def bounded_process(idx, row):
            if self.method == "pseudo-formalisation":
                async with sem:
                    out = await verifier.process_row(row)
                    return idx, out

            else:  # baseline
                async with sem:
                    text, usage = await verifier.process_row(row)
                    return idx, text, usage

        tasks = []

        for key, val in df.items():

            if self.method == "baseline":
                if not val.get("LLM_Full_Output"):
                    for i in range(verifier.n):
                        tasks.append(asyncio.create_task(bounded_process(key, val)))
                else:
                    current_n = len(val.get("LLM_Full_Output"))
                    for i in range(current_n, verifier.n):
                        tasks.append(asyncio.create_task(bounded_process(key, val)))

            elif self.method == "pseudo-formalisation":
                n_verification_calls = len(val.get("LLM_Full_Output", []))
                for i in range(n_verification_calls, verifier.n):
                    ro = {
                        "proof": val["Response"],
                        "problem": val.get("Problem"),
                        "original_steps": val.get("Original_Steps"),
                    }
                    tasks.append(asyncio.create_task(bounded_process(key, ro)))

        print(
            f"🚀 Starting evaluation of {len(tasks)} samples (concurrency={self.concurrency})..."
        )

        if tasks:
            i = 0
            for coro in tqdm(
                asyncio.as_completed(tasks),
                total=len(tasks),
                desc="Grading",
                unit="row",
            ):
                i += 1

                if self.method == "pseudo-formalisation":
                    idx, out = await coro
                    if "LLM_Full_Output" not in df[idx].keys():
                        df[idx]["LLM_Full_Output"] = []
                    df[idx]["LLM_Full_Output"].append(out)
                    if out.get("parse_failed"):
                        df[idx]["parse_failed"] = True

                else:  # baseline
                    idx, text, usage = await coro
                    score = parse_score(text)

                    if verifier.n > 1:
                        if "LLM_Full_Output" not in df[idx].keys():
                            df[idx]["LLM_Full_Output"] = []

                        df[idx]["LLM_Full_Output"].append(
                            {"text": text, "usage": usage, "score": score}
                        )

                    else:
                        df[idx]["LLM_Full_Output"] = [
                            {"text": text, "usage": usage, "score": score}
                        ]

                if i % 50 == 0:
                    save_json(df, output / "grading_full_output.json")

            save_json(df, output / "grading_full_output.json")

            # Report parse failures for pseudo-formalisation
            if self.method == "pseudo-formalisation":
                n_parse_failed = sum(1 for v in df.values() if v.get("parse_failed"))
                if n_parse_failed > 0:
                    print(
                        f"⚠ {n_parse_failed}/{len(df)} proofs failed to parse after retries and were not verified."
                    )

        return df
