"""Verifiers for FirstProofGradingBench (paper Section 5, Appendix F).

Both methods run one ROLLOUT per `process_row` call, and every rollout starts
with a theorem check: does the submission prove the problem as posed, or only
a weaker / conditional / different statement? If the theorem check fires, the
rollout's output is the theorem check's prediction alone and nothing else runs.
Otherwise:

  FirstProofBaseline      one LLM-as-judge referee call over the whole
                          submission, with web search.
  FirstProofPFVerifier    block verification of every block of a FIXED
                          Pseudo-Formal proof (the same PF is reused by every
                          rollout), with web search, then a calibrator that
                          keeps or rejects each flagged block.

Web search blocks GitHub and the First Proof site. `web_trace` records every
search, opened page and citation; a citation of a blocked domain is treated as
a failed call and retried.

`judge_rollout` is the verifier auto eval: one GPT-5.5 call per rollout that
matches that rollout's predictions to the ground-truth errors.

All models run at gpt-5.5 / high effort by default, as in the paper.
"""

import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from src.verifier import Verifier
from src.verifier.arxiv_complex_verifier import (
    ArxivComplexPseudoFormalisationVerifier,
    parse_rewritten_arxiv_complex,
)
from src.verifier.firstproof_prompts import (
    BASELINE_REFEREE_PROMPT_WEB,
    COMPONENT_VERIFY_PROMPT,
    PF_META_VERIFY_PROMPT_V4,
    TM_BASELINE_PROMPT,
    TM_PF_PROMPT,
    V4_2_DIRECTIVE,
    WHOLE_REVIEW_JUDGE_PROMPT_V2,
)

# The verifiers must not read the First Proof referee reports (the ground truth).
BLOCKED_DOMAINS: List[str] = [
    "1stproof.org",
    "github.com",
    "githubusercontent.com",
    "github.io",
]

# Output-token caps (reasoning included) and per-call timeouts, as in the paper runs.
TM_MAX_OUT = 16000
TM_CALL_TIMEOUT = 900
BASELINE_MAX_OUT = 50000
BASELINE_CALL_TIMEOUT = 1800
BV_MAX_OUT = 16000
BV_CALL_TIMEOUT = 420
CALIB_MAX_OUT = 30000
CALIB_CALL_TIMEOUT = 1800
JUDGE_MAX_OUT = 16000

# Label the theorem-check "block" carries in the baseline arm, where there is no
# PF theorem block to point at.
_BASELINE_TM_STATEMENT = "(whole submission vs original problem)"


# ── Web-search audit ─────────────────────────────────────────────────────────


def web_trace(resp) -> Dict[str, Any]:
    """Search audit trail for one Responses-API call.

    `opened` are pages the model fetched, `found` are URLs a search surfaced,
    `queries` are its searches and `sources` are pages it cited. The
    blocked-domain filter constrains search results only, so an opened blocked
    domain is recorded (and printed) rather than raised; a CITED blocked domain
    raises.
    """
    queries: List[str] = []
    sources: List[Dict[str, str]] = []
    opened: List[str] = []
    found: List[str] = []
    actions: Dict[str, int] = {}
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", None) == "web_search_call":
            action = getattr(item, "action", None)
            kind = getattr(action, "type", None) if action else None
            actions[str(kind)] = actions.get(str(kind), 0) + 1
            for q in (getattr(action, "queries", None) or []) if action else []:
                if q not in queries:
                    queries.append(q)
            q = getattr(action, "query", None) if action else None
            if q and q not in queries:
                queries.append(q)
            url = getattr(action, "url", None) if action else None
            if url:
                opened.append(url)
            for s in (getattr(action, "sources", None) or []) if action else []:
                u = getattr(s, "url", None)
                if u:
                    found.append(u)
        for c in getattr(item, "content", None) or []:
            for a in getattr(c, "annotations", None) or []:
                url = getattr(a, "url", None)
                if url:
                    sources.append({"url": url, "title": getattr(a, "title", None) or ""})
    hit = lambda u: any(d in u for d in BLOCKED_DOMAINS)  # noqa: E731
    bad_open = [u for u in opened if hit(u)]
    bad_found = [u for u in found if hit(u)]
    if bad_open or bad_found:
        print(f"  !! BLOCKED DOMAIN reached: opened={bad_open} found={bad_found}",
              file=sys.stderr, flush=True)
    bad = [s["url"] for s in sources if hit(s["url"])]
    if bad:
        raise RuntimeError(f"BLOCKED DOMAIN cited despite the filter: {bad}")
    return {"queries": queries, "sources": sources, "n_citations": len(sources),
            "opened": opened, "found": found, "actions": actions,
            "blocked_opened": bad_open, "blocked_found": bad_found}


# ── Output parsing and quote localisation ────────────────────────────────────


def _last_tag(out: str, tag: str) -> Optional[str]:
    tags = re.findall(rf"<{tag}>\s*(.*?)\s*</{tag}>", out, re.DOTALL)
    return tags[-1].strip() if tags else None


def _strict_verdict(out: str) -> str:
    """CORRECT | INCORRECT from the last <verdict> tag; anything else raises
    (and so is retried) rather than defaulting."""
    verdict = (_last_tag(out, "verdict") or "").upper()
    if verdict not in ("CORRECT", "INCORRECT"):
        raise RuntimeError(f"no explicit <verdict> tag (got {verdict!r})")
    return verdict


def _error_description(out: str) -> str:
    m = re.search(r"<error_description>(.*?)</error_description>", out, re.DOTALL)
    d = m.group(1).strip() if m else ""
    return d or "(no description parsed)"


def parse_baseline_errors(output: str) -> Tuple[List[Dict[str, str]], str]:
    """{location, quote, description} dicts from the referee's XML output.
    Status: ok | empty_response | no_xml_blocks | schema_error | no_error_reported."""
    if not output:
        return [], "empty_response"
    blocks = re.findall(r"<error>(.*?)</error>", output, re.DOTALL)
    if not blocks:
        if re.search(r"<errors>\s*</errors>", output, re.DOTALL):
            return [], "no_error_reported"
        return [], "no_xml_blocks"
    cleaned: List[Dict[str, str]] = []
    for block in blocks:
        fields = {
            name: (m.group(1).strip()
                   if (m := re.search(rf"<{name}>(.*?)</{name}>", block, re.DOTALL))
                   else "")
            for name in ("location", "quote", "description")
        }
        if fields["quote"] or fields["description"]:
            cleaned.append(fields)
    if not cleaned:
        return [], "schema_error"
    return cleaned, "ok"


def locate_block(proof: str, block: str) -> Tuple[str, int, int]:
    """(status, start, end) of `block` within `proof`: exact match first, then a
    whitespace-insensitive match mapped back to original offsets."""
    if not block:
        return ("not_found", -1, -1)
    idx = proof.find(block)
    if idx != -1:
        return ("exact", idx, idx + len(block))
    norm_chars, orig_idx = [], []
    prev_space = False
    for i, ch in enumerate(proof):
        if ch.isspace():
            if prev_space:
                continue
            norm_chars.append(" ")
            orig_idx.append(i)
            prev_space = True
        else:
            norm_chars.append(ch)
            orig_idx.append(i)
            prev_space = False
    nb = re.sub(r"\s+", " ", block).strip()
    j = "".join(norm_chars).find(nb)
    if j == -1 or not nb:
        return ("not_found", -1, -1)
    start = orig_idx[j]
    end = orig_idx[min(j + len(nb) - 1, len(orig_idx) - 1)] + 1
    return ("fuzzy", start, end)


_PF_SCAFFOLD = re.compile(
    r"^(assumptions?\s*/\s*conditions|statement\s*:|original label|title:|"
    r"abstract:|section heading|external citations?)",
    re.IGNORECASE,
)


def _block_fragments(block_text: str) -> List[str]:
    """Candidate verbatim fragments of a PF block: display-math runs, then
    prose sentences >= 40 chars with PF scaffolding lines dropped."""
    frags: List[str] = []
    for m in re.finditer(r"\\\[(.*?)\\\]", block_text, re.DOTALL):
        inner = m.group(1).strip()
        if len(inner) >= 20:
            frags.append(inner)
    prose = re.sub(r"\\\[(.*?)\\\]", " ", block_text, flags=re.DOTALL)
    for line in re.split(r"(?<=[.;:])\s+|\n", prose):
        line = line.strip()
        if len(line) >= 40 and not _PF_SCAFFOLD.match(line):
            frags.append(line)
    return frags


def _norm_math_index(proof: str) -> Tuple[str, List[int]]:
    """Canonical form of the proof (\\( \\) -> $, whitespace dropped) plus a map
    from canonical chars to original offsets."""
    chars, index = [], []
    i = 0
    while i < len(proof):
        if proof[i : i + 2] in (r"\(", r"\)"):
            chars.append("$")
            index.append(i)
            i += 2
            continue
        if not proof[i].isspace():
            chars.append(proof[i])
            index.append(i)
        i += 1
    return "".join(chars), index


def map_block_to_proof_quote(block_text: str, proof: str) -> Dict[str, Any]:
    """Map a PF block onto the original proof: try each fragment exact ->
    whitespace-fuzzy -> math-normalised, and return the LONGEST matched
    fragment as the prediction's verbatim quote (with offsets)."""
    proof_norm, norm_index = _norm_math_index(proof)
    best: Dict[str, Any] = {"quote": "", "match": "not_found", "start": -1, "end": -1}
    for frag in _block_fragments(block_text):
        status, start, end = locate_block(proof, frag)
        if status == "not_found":
            nf = frag.replace(r"\(", "$").replace(r"\)", "$")
            nf = re.sub(r"\s+", "", nf)
            if len(nf) >= 20:
                j = proof_norm.find(nf)
                if j != -1:
                    status = "norm"
                    start = norm_index[j]
                    end = norm_index[min(j + len(nf) - 1, len(norm_index) - 1)] + 1
        if status != "not_found" and (end - start) > (best["end"] - best["start"]):
            best = {"quote": proof[start:end], "match": status, "start": start, "end": end}
    return best


def parse_calibrator_kept(out: str) -> List[str]:
    """Labels of the flags the calibrator judged `genuine`. Every flag must be
    adjudicated as genuine or rewrite_artifact; anything else raises."""
    m = re.search(r"<errors>(.*?)</errors>", out, re.DOTALL)
    if not m:
        raise RuntimeError("calibrator output has no <errors> block")
    blocks = re.findall(r"<error>(.*?)</error>", m.group(1), re.DOTALL)
    if not blocks:
        raise RuntimeError("calibrator output adjudicated no flags")
    kept = []
    for b in blocks:
        c = re.search(r"<component>(.*?)</component>", b, re.DOTALL)
        v = re.search(r"<verdict>(.*?)</verdict>", b, re.DOTALL)
        if not c or not v:
            raise RuntimeError("calibrator <error> without component/verdict")
        verdict = v.group(1).strip().lower()
        if verdict not in ("genuine", "rewrite_artifact"):
            raise RuntimeError(f"calibrator verdict {verdict!r} not allowed")
        if verdict == "genuine":
            kept.append(c.group(1).strip())
    return kept


# ── Shared model-call machinery ──────────────────────────────────────────────


class _FirstProofBase(Verifier):
    def __init__(
        self,
        n: int = 4,
        model: str = "gpt-5.5",
        effort: str = "high",
        max_tries: int = 4,
        concurrency: int = 64,
    ):
        # No SDK timeout or SDK retries: long web-search calls exceed the SDK's
        # 600s default, and retries are handled here.
        self.client = AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"), timeout=None, max_retries=0
        )
        self.n = n
        self.model = model
        self.effort = effort
        self.max_tries = max_tries
        self.sem = asyncio.Semaphore(concurrency)

    def _request(self, prompt: str, max_out: int, web: bool) -> Dict[str, Any]:
        req = dict(
            model=self.model,
            input=[{"role": "user", "content": prompt}],
            text={"format": {"type": "text"}},
            reasoning={"effort": self.effort},
            max_output_tokens=max_out,
        )
        if web:
            req["tools"] = [{"type": "web_search",
                             "filters": {"blocked_domains": BLOCKED_DOMAINS}}]
            req["tool_choice"] = "auto"
        return req

    async def _background(self, req: Dict[str, Any], deadline: float, label: str):
        """Run one Responses call in background mode and poll it, instead of
        holding one connection open through long silent reasoning gaps (which
        can hang indefinitely on long web-search calls)."""
        r = await self.client.responses.create(background=True, timeout=120.0, **req)
        t0 = time.perf_counter()
        fails = 0
        while r.status in ("queued", "in_progress"):
            if time.perf_counter() - t0 > deadline:
                try:
                    await self.client.responses.cancel(r.id, timeout=60.0)
                except Exception:
                    pass
                raise TimeoutError(f"{label} background {r.id} exceeded {deadline:.0f}s")
            await asyncio.sleep(10.0)
            try:
                r = await self.client.responses.retrieve(r.id, timeout=60.0)
                fails = 0
            except Exception:
                fails += 1
                if fails >= 10:
                    raise
        if r.status != "completed":
            raise RuntimeError(f"{label} background {r.id} status={r.status} "
                               f"error={getattr(r, 'error', None)}")
        return r

    async def _call(
        self,
        prompt: str,
        *,
        max_out: int,
        timeout: Optional[float],
        web: bool = False,
        background: bool = False,
        check=None,
        label: str = "",
    ) -> Tuple[str, Dict[str, Any], Any]:
        """(output text, usage, check(output)). `check` validates/parses the
        output; a failure there, an empty output, an API error or a cited
        blocked domain all retry, up to max_tries."""
        req = self._request(prompt, max_out, web)
        async with self.sem:
            last: Optional[Exception] = None
            for attempt in range(self.max_tries):
                try:
                    if background:
                        resp = await self._background(req, timeout or 3600.0, label)
                    else:
                        c = self.client.responses.create(**req)
                        resp = await (asyncio.wait_for(c, timeout) if timeout else c)
                    out = resp.output_text or ""
                    if not out.strip():
                        raise RuntimeError("empty output text")
                    trace = web_trace(resp) if web else None
                    parsed = check(out) if check else None
                    u = getattr(resp, "usage", None)
                    usage = {
                        "input_tokens": getattr(u, "input_tokens", 0) or 0,
                        "output_tokens": getattr(u, "output_tokens", 0) or 0,
                        "web_trace": trace,
                    }
                    return out, usage, parsed
                except Exception as e:
                    last = e
                    print(f"  retry {label} ({attempt + 1}/{self.max_tries}): "
                          f"{type(e).__name__}: {str(e)[:120]}", file=sys.stderr, flush=True)
                    if attempt < self.max_tries - 1:
                        await asyncio.sleep(3 * (attempt + 1))
            raise RuntimeError(f"{label} failed after {self.max_tries} attempts") from last

    def _tm_prediction(self, out: str, block_text: str, proof: str) -> Dict[str, Any]:
        return {"source": "theorem_check", "pf_block": "Theorem 1",
                "description": _error_description(out),
                **map_block_to_proof_quote(block_text, proof)}

    async def judge_rollout(
        self, row: Dict[str, Any], predictions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Verifier auto eval for ONE rollout: which ground-truth errors (by
        index into row["errors"]) each prediction matches."""
        golds = row["errors"]
        if not predictions:
            return {"pred_matches": [], "output": None}
        if not golds:
            return {"pred_matches": [[] for _ in predictions], "output": None}
        gold_list = "\n\n".join(
            f"[{i}] Excerpt containing the error (verbatim):\n{g['errored_block']}\n"
            f"    Description: {g['error_description']}"
            for i, g in enumerate(golds)
        )
        pred_parts = []
        for i, p in enumerate(predictions):
            lines = [f"[{i}] Quote (verbatim from proof): {p.get('quote') or '(none)'}"]
            if p.get("location"):
                lines.append(f"    Location note: {p['location']}")
            lines.append(f"    Description: {p.get('description') or '(none)'}")
            pred_parts.append("\n".join(lines))
        prompt = WHOLE_REVIEW_JUDGE_PROMPT_V2.format(
            proof=row["proof_tex"], gold_list=gold_list, pred_list="\n\n".join(pred_parts)
        )

        def check(out: str) -> List[List[int]]:
            anchor = out.rfind('{"pred_matches"')
            if anchor == -1:
                raise RuntimeError("no pred_matches JSON in judge output")
            data, _ = json.JSONDecoder().raw_decode(out[anchor:])
            matches = data.get("pred_matches")
            if not isinstance(matches, list) or len(matches) != len(predictions):
                raise RuntimeError(f"pred_matches has wrong length: {matches!r}")
            cleaned = []
            for entry in matches:
                idxs = sorted(set(int(i) for i in entry))
                if any(i < 0 or i >= len(golds) for i in idxs):
                    raise RuntimeError(f"gold index out of range in {idxs}")
                cleaned.append(idxs)
            return cleaned

        out, usage, matches = await self._call(
            prompt, max_out=JUDGE_MAX_OUT, timeout=None, check=check,
            label=f"judge {row['id']}")
        return {"pred_matches": matches, "output": out, "usage": usage}


# ── Baseline ─────────────────────────────────────────────────────────────────


class FirstProofBaseline(_FirstProofBase):
    """Theorem check (whole submission), then one web-search referee call."""

    async def process_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        sid, proof = row["id"], row["proof_tex"]
        tm_out, tm_usage, tm_verdict = await self._call(
            TM_BASELINE_PROMPT.format(original=row["problem_latex"], proof=proof),
            max_out=TM_MAX_OUT, timeout=TM_CALL_TIMEOUT, check=_strict_verdict,
            label=f"theorem check {sid}")
        result: Dict[str, Any] = {
            "theorem_check": {"verdict": tm_verdict, "output": tm_out, "usage": tm_usage},
            "gated": tm_verdict == "INCORRECT",
            "referee": None,
        }
        if result["gated"]:
            result["predictions"] = [self._tm_prediction(tm_out, _BASELINE_TM_STATEMENT, proof)]
            return result

        def check(out: str):
            errors, status = parse_baseline_errors(out)
            if status not in ("ok", "no_error_reported"):
                raise RuntimeError(f"referee output unparseable (status={status})")
            return errors, status

        out, usage, (errors, status) = await self._call(
            BASELINE_REFEREE_PROMPT_WEB.format(paper_tex=proof),
            max_out=BASELINE_MAX_OUT, timeout=BASELINE_CALL_TIMEOUT, web=True,
            background=True, check=check, label=f"referee {sid}")
        for e in errors:
            e["match"], e["start"], e["end"] = locate_block(proof, e["quote"])
            e["source"] = "referee"
        result["referee"] = {"output": out, "status": status, "usage": usage}
        result["predictions"] = errors
        return result


# ── Pseudo-Formalization ─────────────────────────────────────────────────────


class FirstProofPFVerifier(_FirstProofBase):
    """Theorem check (PF theorem block), then block verification of every PF
    block with web search, then the calibrator over this rollout's flags.

    row["pf_text"] is the proof's (fixed) Pseudo-Formal rewrite.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        # Used only for its block-input assembly (context / established results).
        self._blocks = ArxivComplexPseudoFormalisationVerifier(
            model=self.model, effort=self.effort, max_tokens=BV_MAX_OUT, n_verifications=1)

    async def process_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        sid, proof, pf_text = row["id"], row["proof_tex"], row["pf_text"]
        decomp = parse_rewritten_arxiv_complex(pf_text)
        binputs = self._blocks._build_block_inputs(decomp)

        tkey = next(k for k in sorted(b[0] for b in binputs) if k.startswith("theorem_"))
        _, _, t_stmt, t_proof, _, t_est = next(b for b in binputs if b[0] == tkey)
        tm_out, tm_usage, tm_verdict = await self._call(
            TM_PF_PROMPT.format(original=row["problem_latex"], claimed=t_stmt,
                                established="\n\n".join(t_est) if t_est else "None",
                                proof=t_proof),
            max_out=TM_MAX_OUT, timeout=TM_CALL_TIMEOUT, check=_strict_verdict,
            label=f"theorem check {sid}")
        result: Dict[str, Any] = {
            "theorem_check": {"verdict": tm_verdict, "output": tm_out, "usage": tm_usage},
            "gated": tm_verdict == "INCORRECT",
            "blocks": None,
            "calibrator": None,
        }
        if result["gated"]:
            tm_block = "\n".join(p for p in (t_stmt, t_proof) if p)
            result["predictions"] = [self._tm_prediction(tm_out, tm_block, proof)]
            return result

        async def verify_block(key, label, stmt, bproof, ctx, est):
            prompt = V4_2_DIRECTIVE + COMPONENT_VERIFY_PROMPT.format(
                contexts="\n\n".join(ctx) if ctx else "None",
                established_results="\n\n".join(est) if est else "None",
                assertion=f"{label}: {stmt}",
                proof=bproof,
            )
            out, usage, verdict = await self._call(
                prompt, max_out=BV_MAX_OUT, timeout=BV_CALL_TIMEOUT, web=True,
                check=_strict_verdict, label=f"block {sid} {label}")
            return {"key": key, "label": label, "statement": stmt, "proof": bproof,
                    "verdict": verdict, "output": out, "usage": usage}

        blocks = await asyncio.gather(*(verify_block(*b) for b in binputs))
        result["blocks"] = blocks
        flagged = [b for b in blocks if b["verdict"] == "INCORRECT"]

        kept: set = set()
        if flagged:
            prompt = PF_META_VERIFY_PROMPT_V4.format(
                original_proof=proof,
                rewritten_proof=pf_text,
                errors="\n\n---\n\n".join(f"[{b['label']}]\n{b['output']}" for b in flagged),
            )
            out, usage, labels = await self._call(
                prompt, max_out=CALIB_MAX_OUT, timeout=CALIB_CALL_TIMEOUT,
                check=parse_calibrator_kept, label=f"calibrator {sid}")
            have = {b["label"] for b in flagged}
            kept = {k for k in labels if k in have}
            result["calibrator"] = {"output": out, "usage": usage,
                                    "flagged": sorted(have), "kept": sorted(kept)}

        # One prediction per flagged block. All are judged; the calibrated score
        # counts only those the calibrator kept (see scripts/firstproof_metrics.py).
        result["predictions"] = [
            {"source": "block_verifier", "pf_block": b["label"],
             "description": _error_description(b["output"]),
             "calibrator_kept": b["label"] in kept,
             **map_block_to_proof_quote(
                 "\n".join(p for p in (b["statement"], b["proof"]) if p), proof)}
            for b in flagged
        ]
        return result
