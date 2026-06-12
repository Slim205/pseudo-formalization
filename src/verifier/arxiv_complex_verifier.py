"""ArxivComplexPseudoFormalisationVerifier — combines the IMO Complex
verifier mechanism (deep 4-layer decomposition + DEPS-based citation
graph + multi-vote + global-block-check + reverification) with the
arxiv multi-theorem schema and PDF support.

Differences from ``ComplexPseudoFormalisationVerifier``:
- Multiple top-level theorems via ``<THEOREM_STATEMENT id="N">`` with a
  shared proposition pool below.
- Rewrite + meta-verify accept a PDF attachment alongside the LaTeX.
- Web search is disabled in the component verifier (so the model cannot
  short-circuit by locating the paper online).
- The calibrator emits an arxiv-baseline-style ``<errors>...</errors>``
  list with locations keyed to PDF-rendered labels, instead of an IMO
  0-7 score.

DEPS conventions:
- For non-theorem proofs, DEPS ids are dotted (``id="1.1.1"``) and may
  cite dotted blocks only.
- For theorem proofs, the DEPS tag itself uses ``id="theorem_N"`` and
  the body may contain proposition ids (``"1, 3"``) plus references to
  earlier theorems written as ``"theorem_M"`` for ``M < N``.
"""

import os
import asyncio
import re
import time
import json
from typing import Any, Dict, Tuple, Optional, List

from openai import AsyncOpenAI

from src.verifier import Verifier
from src.verifier.arxiv_complex_prompts import (
    ARXIV_COMPLEX_REWRITE_PROMPT,
    ARXIV_COMPLEX_REGENERATE_REWRITE_PROMPT,
    ARXIV_COMPLEX_COMPONENT_VERIFY_PROMPT,
    ARXIV_COMPLEX_META_VERIFY_PROMPT,
    ARXIV_COMPONENT_FAITHFULNESS_PROMPT,
    GLOBAL_BLOCK_CHECK_PROMPT,
)
from src.verifier.complex_verifier import (
    _extract_tag,
    _extract_all_ids,
    _parse_dotted_id,
    _ensure_parent_chain,
    _normalize_for_comparison,
    _usage_to_dict,
    _parse_deps_tags,
    _lookup_numeric,
    _parse_faithfulness_verdict,
    _extract_missing_tags,
    _parse_global_check_json,
    _block_key_to_label,
    _block_key_to_dotted_id,
)
from src.verifier.verifier import ArxivVerifierBaseline


# =============================================================================
# Helpers — multi-theorem aware
# =============================================================================

_THEOREM_REF_RE = re.compile(r"^theorem_(\d+)$", re.IGNORECASE)


def _parse_theorem_ref(s: str) -> Optional[int]:
    """Return the integer N if *s* is the form ``theorem_N``, else None."""
    if not isinstance(s, str):
        return None
    m = _THEOREM_REF_RE.match(s.strip())
    return int(m.group(1)) if m else None


def parse_rewritten_arxiv_complex(text: str) -> Dict[str, Any]:
    """Parse a rewrite using the arxiv-complex schema:
    multi-theorem (``<THEOREM_STATEMENT id="N">``) + 4 layers below
    (Proposition / Lemma / Claim / Fact) + DEPS tags.

    Returns a dict with keys: ``theorems``, ``propositions``, ``deps``,
    ``preamble``, ``_raw_sections``.
    """
    # ── Theorems ──────────────────────────────────────────────────
    theorem_ids = _extract_all_ids(text, "THEOREM_STATEMENT")
    theorems: Dict[int, Dict[str, Any]] = {}
    for tid_str in theorem_ids:
        try:
            tid = int(tid_str)
        except ValueError:
            continue
        theorems[tid] = {
            "statement": _extract_tag(text, "THEOREM_STATEMENT", tid_str) or "",
            "proof": _extract_tag(text, "THEOREM_PROOF", tid_str),
        }

    # ── Propositions (top-level, integer ids; shared pool) ────────
    prop_ids = _extract_all_ids(text, "PROPOSITION_STATEMENT")
    propositions: Dict[int, Dict[str, Any]] = {}
    for pid_str in prop_ids:
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        propositions[pid] = {
            "statement": _extract_tag(text, "PROPOSITION_STATEMENT", pid_str) or "",
            "proof": _extract_tag(text, "PROPOSITION_PROOF", pid_str),
            "lemmas": {},
        }

    # ── Lemmas ────────────────────────────────────────────────────
    for lid_str in _extract_all_ids(text, "LEMMA_STATEMENT"):
        parts = _parse_dotted_id(lid_str)
        if not parts:
            continue
        pid = parts[0]
        lid = parts[1] if len(parts) > 1 else 1
        _ensure_parent_chain(propositions, [pid])
        propositions[pid]["lemmas"][lid] = {
            "statement": _extract_tag(text, "LEMMA_STATEMENT", lid_str) or "",
            "proof": _extract_tag(text, "LEMMA_PROOF", lid_str),
            "claims": {},
        }

    # ── Claims ────────────────────────────────────────────────────
    for cid_str in _extract_all_ids(text, "CLAIM_STATEMENT"):
        parts = _parse_dotted_id(cid_str)
        if not parts:
            continue
        pid = parts[0]
        lid = parts[1] if len(parts) > 1 else 1
        cid = parts[2] if len(parts) > 2 else 1
        _ensure_parent_chain(propositions, [pid, lid])
        propositions[pid]["lemmas"][lid]["claims"][cid] = {
            "statement": _extract_tag(text, "CLAIM_STATEMENT", cid_str) or "",
            "proof": _extract_tag(text, "CLAIM_PROOF", cid_str),
            "facts": {},
        }

    # ── Facts ─────────────────────────────────────────────────────
    for fid_str in _extract_all_ids(text, "FACT_STATEMENT"):
        parts = _parse_dotted_id(fid_str)
        if not parts:
            continue
        pid = parts[0]
        lid = parts[1] if len(parts) > 1 else 1
        cid = parts[2] if len(parts) > 2 else 1
        fid = parts[3] if len(parts) > 3 else 1
        _ensure_parent_chain(propositions, [pid, lid, cid])
        propositions[pid]["lemmas"][lid]["claims"][cid]["facts"][fid] = {
            "statement": _extract_tag(text, "FACT_STATEMENT", fid_str) or "",
            "proof": _extract_tag(text, "FACT_PROOF", fid_str),
        }

    # ── DEPS ──────────────────────────────────────────────────────
    deps = _parse_deps_tags(text)

    # ── Raw sections ──────────────────────────────────────────────
    all_tags = "|".join([
        "THEOREM_STATEMENT", "PROPOSITION_STATEMENT", "LEMMA_STATEMENT",
        "CLAIM_STATEMENT", "FACT_STATEMENT", "FACT_PROOF", "CLAIM_PROOF",
        "LEMMA_PROOF", "PROPOSITION_PROOF", "THEOREM_PROOF",
    ])
    raw_pattern = rf"<({all_tags})(?:\s[^>]*)?>(.+?)</\1>"
    raw_sections = [m.group(2).strip() for m in re.finditer(raw_pattern, text, re.DOTALL)]

    return {
        "preamble": "",
        "theorems": theorems,
        "propositions": propositions,
        "deps": deps,
        "_raw_sections": raw_sections,
    }


def _format_block_for_established_arxiv(
    decomposition: Dict[str, Any], id_str: str, include_proof: bool = False
) -> Optional[str]:
    """Resolve a block id to a formatted established-result line.

    Recognises ``theorem_N`` for theorem references and dotted ids for
    propositions/lemmas/claims/facts in the shared pool. Returns None
    when the id does not resolve.
    """
    s = id_str.strip()

    def _with_proof(line: str, proof_val: Optional[str]) -> str:
        if include_proof and proof_val:
            return f"{line}\nProof: {proof_val}"
        return line

    tid = _parse_theorem_ref(s)
    if tid is not None:
        thm = _lookup_numeric(decomposition.get("theorems", {}), tid)
        if thm is None:
            return None
        return _with_proof(f"Theorem {tid}: {thm['statement']}", thm.get("proof"))

    parts = _parse_dotted_id(s)
    if not parts:
        return None
    props = decomposition.get("propositions", {})
    pid = parts[0]
    prop = _lookup_numeric(props, pid)
    if prop is None:
        return None
    if len(parts) == 1:
        return _with_proof(f"Proposition {pid}: {prop['statement']}", prop.get("proof"))
    lid = parts[1]
    lemma = _lookup_numeric(prop.get("lemmas", {}), lid)
    if lemma is None:
        return None
    if len(parts) == 2:
        return _with_proof(f"Lemma {pid}.{lid}: {lemma['statement']}", lemma.get("proof"))
    cid = parts[2]
    claim = _lookup_numeric(lemma.get("claims", {}), cid)
    if claim is None:
        return None
    if len(parts) == 3:
        return _with_proof(f"Claim {pid}.{lid}.{cid}: {claim['statement']}", claim.get("proof"))
    fid = parts[3]
    fact = _lookup_numeric(claim.get("facts", {}), fid)
    if fact is None:
        return None
    return _with_proof(f"Fact {pid}.{lid}.{cid}.{fid}: {fact['statement']}", fact.get("proof"))


def _check_deps_ordering_arxiv(decomposition: Dict[str, Any]) -> List[str]:
    """Validate every DEPS citation under the multi-theorem schema.

    Rules:
      - ``theorem_N`` DEPS may cite any dotted-id block (any proposition
        and its descendants) and any ``theorem_M`` with M < N.
      - A dotted-id DEPS may cite dotted-ids only (no theorem refs), and
        the citation must be either a proper descendant of the citing
        block or earlier in document order outside the citing subtree.
        It cannot be the citing block itself or one of its ancestors.
      - Every cited id must resolve to an existing block.
    """
    deps_map: Dict[str, List[str]] = decomposition.get("deps", {})
    errors: List[str] = []

    def _exists(id_str: str) -> bool:
        return _format_block_for_established_arxiv(decomposition, id_str) is not None

    def _is_strict_prefix(prefix: Tuple[int, ...], full: Tuple[int, ...]) -> bool:
        return len(prefix) < len(full) and full[: len(prefix)] == prefix

    for citing_id, cited_list in deps_map.items():
        citing_thm = _parse_theorem_ref(citing_id)

        if citing_thm is not None:
            for cited_id in cited_list:
                if not _exists(cited_id):
                    errors.append(
                        f"DEPS id='{citing_id}': cited id '{cited_id}' does not correspond to any block"
                    )
                    continue
                cited_thm = _parse_theorem_ref(cited_id)
                if cited_thm is not None and cited_thm >= citing_thm:
                    errors.append(
                        f"DEPS id='{citing_id}': cited theorem 'theorem_{cited_thm}' must be strictly earlier than the citing theorem"
                    )
            continue

        # Non-theorem citing block: dotted id required.
        try:
            citing_parts = _parse_dotted_id(citing_id) or []
        except (ValueError, TypeError):
            citing_parts = []
        if not citing_parts:
            errors.append(f"DEPS id='{citing_id}': citing block id is not a valid dotted id")
            continue
        citing_tuple = tuple(citing_parts)

        for cited_id in cited_list:
            if not _exists(cited_id):
                errors.append(
                    f"DEPS id='{citing_id}': cited id '{cited_id}' does not correspond to any block"
                )
                continue
            if _parse_theorem_ref(cited_id) is not None:
                errors.append(
                    f"DEPS id='{citing_id}': non-theorem block cannot cite a theorem statement ('{cited_id}')"
                )
                continue
            try:
                cited_parts = _parse_dotted_id(cited_id) or []
            except (ValueError, TypeError):
                cited_parts = []
            if not cited_parts:
                errors.append(
                    f"DEPS id='{citing_id}': cited id '{cited_id}' is not a valid dotted id"
                )
                continue
            cited_tuple = tuple(cited_parts)
            if cited_tuple == citing_tuple:
                errors.append(f"DEPS id='{citing_id}': block cannot cite itself")
                continue
            if _is_strict_prefix(cited_tuple, citing_tuple):
                errors.append(
                    f"DEPS id='{citing_id}': cited id '{cited_id}' is an ancestor of the citing block"
                )
                continue
            if cited_tuple > citing_tuple and not _is_strict_prefix(citing_tuple, cited_tuple):
                errors.append(
                    f"DEPS id='{citing_id}': cited id '{cited_id}' is not before the citing block and is not a descendant of it"
                )

    return errors


def verify_arxiv_complex_parse_roundtrip(
    original: str, decomposition: Dict[str, Any], is_identical: bool = False
) -> Dict[str, Any]:
    """Structural-completeness check (no roundtrip identity by default
    because whole-paper rewrites paraphrase prose). Validates the
    multi-theorem 4-layer tree plus DEPS citation rules."""
    errors: List[str] = []

    if not decomposition.get("theorems"):
        errors.append("No theorems found")
    else:
        for tid, thm in sorted(decomposition["theorems"].items()):
            if not thm["statement"]:
                errors.append(f"Missing THEOREM_STATEMENT id={tid}")
            if thm["proof"] is None:
                errors.append(f"Missing THEOREM_PROOF id={tid}")

    if not decomposition.get("propositions"):
        errors.append("No propositions found")

    for pid, prop in sorted(decomposition["propositions"].items()):
        if not prop["statement"]:
            errors.append(f"Missing PROPOSITION_STATEMENT id={pid}")
        if prop["proof"] is None:
            errors.append(f"Missing PROPOSITION_PROOF id={pid}")
        for lid, lemma in sorted(prop.get("lemmas", {}).items()):
            if not lemma["statement"]:
                errors.append(f"Missing LEMMA_STATEMENT id={pid}.{lid}")
            if lemma["proof"] is None:
                errors.append(f"Missing LEMMA_PROOF id={pid}.{lid}")
            for cid, claim in sorted(lemma.get("claims", {}).items()):
                if not claim["statement"]:
                    errors.append(f"Missing CLAIM_STATEMENT id={pid}.{lid}.{cid}")
                if claim["proof"] is None:
                    errors.append(f"Missing CLAIM_PROOF id={pid}.{lid}.{cid}")
                for fid, fact in sorted(claim.get("facts", {}).items()):
                    if not fact["statement"]:
                        errors.append(f"Missing FACT_STATEMENT id={pid}.{lid}.{cid}.{fid}")
                    if fact["proof"] is None:
                        errors.append(f"Missing FACT_PROOF id={pid}.{lid}.{cid}.{fid}")

    errors.extend(_check_deps_ordering_arxiv(decomposition))

    if errors:
        return {"success": False, "errors": errors}
    return {"success": True}


# =============================================================================
# Verifier class
# =============================================================================


class ArxivComplexPseudoFormalisationVerifier(Verifier):
    """Multi-theorem 4-layer pseudo-formalisation verifier for arxiv papers.

    Mechanism mirrors ``ComplexPseudoFormalisationVerifier`` (rewrite +
    faithfulness retries + DEPS-augmented per-component verification +
    optional global-block-check + meta-verification). Adapted to:

    1. Multi-theorem schema (shared proposition pool below the theorems).
    2. PDF attachment for rewrite and meta-verify steps.
    3. Web search disabled in component verifier.
    4. Calibrator emits ``<errors>...</errors>`` XML with PDF-rendered
       labels (matching the arxiv baseline output schema), not a 0-7 score.
    """

    def __init__(
        self,
        n: int = 1,
        n_verifications: int = 1,
        max_tries: int = 10,
        base: int = 10,
        meta_verify: bool = True,
        faithfulness_check: bool = True,
        block_verifier: bool = True,
        global_block_check: bool = False,
        m_re: int = 5,
        model: str = "gpt-5.4-mini-2026-03-17",
        effort: str = "medium",
        max_tokens: int = 50000,
        verbose: bool = False,
        max_rewrite_retries: int = 5,
    ):
        self.client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.n = n
        self.n_verifications = n_verifications
        self.max_tries = max_tries
        self.base = base
        self.meta_verify = meta_verify
        self.faithfulness_check = faithfulness_check
        self.block_verifier = block_verifier
        self.global_block_check = global_block_check
        self.m_re = m_re
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.verbose = verbose
        self.max_rewrite_retries = max_rewrite_retries

    # ── Top-level orchestration ────────────────────────────────────
    async def process_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        tex: str = row["tex"]
        pdf_b64: str = row["pdf_b64"]
        pdf_filename: str = row["pdf_filename"]

        # Step 1+2+3: rewrite + parse + faithfulness retry loop.
        rewritten_paper: Optional[str] = None
        rewrite_usage: Optional[Dict[str, Any]] = None
        decomposition: Optional[Dict[str, Any]] = None
        parse_check: Optional[Dict[str, Any]] = None
        faithfulness_results: Optional[Dict[str, Any]] = None
        accumulated_errors: List[str] = []
        failed_rewrites: List[Dict[str, Any]] = []
        rewrite_usages: List[Dict[str, Any]] = []
        previous_rewrite: Optional[str] = None
        last_iteration_errors: List[str] = []
        attempt = 0

        for attempt in range(1, self.max_rewrite_retries + 1):
            if previous_rewrite is None:
                rewrite_prompt_text = ARXIV_COMPLEX_REWRITE_PROMPT.format(paper_tex=tex)
            else:
                errors_text = (
                    "\n".join(f"- {err}" for err in last_iteration_errors)
                    if last_iteration_errors
                    else "- (no specific errors listed)"
                )
                rewrite_prompt_text = ARXIV_COMPLEX_REGENERATE_REWRITE_PROMPT.format(
                    rewrite_instructions=ARXIV_COMPLEX_REWRITE_PROMPT.split(
                        "Now rewrite the following"
                    )[0],
                    original_paper=tex,
                    previous_rewrite=previous_rewrite,
                    errors=errors_text,
                )

            if self.verbose:
                print(f"[verbose] Generating rewrite (attempt {attempt})...")
            rewritten_paper, rewrite_usage = await self._completion_pdf_text(
                prompt_text=rewrite_prompt_text,
                pdf_b64=pdf_b64,
                pdf_filename=pdf_filename,
            )
            rewrite_usages.append({"attempt": attempt, "usage": rewrite_usage})

            try:
                decomposition = parse_rewritten_arxiv_complex(rewritten_paper)
                parse_check = verify_arxiv_complex_parse_roundtrip(
                    rewritten_paper, decomposition, is_identical=False
                )
            except Exception as exc:
                decomposition = {"preamble": "", "theorems": {}, "propositions": {}, "deps": {}, "_raw_sections": []}
                parse_check = {"success": False, "errors": [f"parse exception: {exc!r}"]}
            if not parse_check["success"]:
                continue

            if not self.faithfulness_check:
                break

            if self.verbose:
                print(f"[verbose] Faithfulness check (attempt {attempt})...")
            faithfulness_results = await self._check_faithfulness_arxiv(tex, decomposition)

            unfaithful = [
                (k, res["error_description"])
                for k, res in faithfulness_results.items()
                if res["verdict"] == "UNFAITHFUL"
            ]
            if not unfaithful:
                break

            failed_rewrites.append({
                "attempt": attempt,
                "rewritten_paper": rewritten_paper,
                "rewrite_usage": rewrite_usage,
                "decomposition": decomposition,
                "faithfulness_results": faithfulness_results,
                "unfaithful_components": {k: d for k, d in unfaithful},
            })
            last_iteration_errors = [f"[{k}] {desc}" for k, desc in unfaithful]
            previous_rewrite = rewritten_paper
            accumulated_errors.extend(last_iteration_errors)

        parse_check["attempts"] = attempt
        parse_check["faithfulness_attempts"] = attempt
        parse_check["faithfulness_results"] = faithfulness_results
        parse_check["accumulated_errors"] = accumulated_errors
        parse_check["failed_rewrites"] = failed_rewrites
        parse_check["rewrite_usages"] = rewrite_usages

        # Bail out cleanly if parse never succeeded.
        if not parse_check.get("success"):
            return {
                "rewritten_paper": rewritten_paper,
                "rewrite_usage": rewrite_usage,
                "decomposition": decomposition,
                "parse_check": parse_check,
                "component_verifications": None,
                "meta_verification": None,
                "extracted_errors": [],
                "extracted_locations": [],
                "extraction_status": "parse_failed",
                "parse_failed": True,
                "block_score": 0,
            }

        # Step 4: component verification (text-only, multi-vote, DEPS-augmented).
        if self.block_verifier:
            if self.verbose:
                print("[verbose] Component verification...")
            component_results = await self._verify_components(decomposition)
        else:
            # Skip block verification — assign every block CORRECT.
            component_results = {
                k: {"output": None, "usage": None, "score": 7, "runs": []}
                for k, *_ in self._build_block_inputs(decomposition)
            }

        # Step 5 (optional): global block check + reverification.
        if self.block_verifier and self.global_block_check:
            if self.verbose:
                print("[verbose] Global block check...")
            run_indices = {
                k: list(range(1, len(entry.get("runs") or []) + 1))
                for k, entry in component_results.items()
                if entry.get("score") != 7
            }
            await self._apply_global_block_check(component_results, rewritten_paper, run_indices)
            await self._apply_global_check_reverification(component_results, decomposition)

        # Block-level aggregation for diagnostics.
        all_scores = [r["score"] for r in component_results.values() if r.get("score") is not None]
        block_score = min(all_scores) if all_scores else 0

        # Step 6: meta-verification (PDF + tex + rewrite + flagged errors → XML errors).
        meta_dict: Optional[Dict[str, Any]] = None
        extracted_errors: List[Dict[str, Any]] = []
        extracted_locations: List[str] = []
        extraction_status: str = "no_meta"

        if self.meta_verify:
            error_parts: List[str] = [
                f"[{k}]\n{res['output']}"
                for k, res in component_results.items()
                if res.get("score") == 0
            ]
            errors_text = "\n\n---\n\n".join(error_parts) if error_parts else "(none flagged)"
            meta_prompt_text = ARXIV_COMPLEX_META_VERIFY_PROMPT.format(
                original_paper=tex,
                rewritten_paper=rewritten_paper,
                errors=errors_text,
            )
            if self.verbose:
                print(f"[verbose] Meta-verification ({len(error_parts)} errors flagged)...")
            meta_response, meta_usage = await self._completion_pdf_text(
                prompt_text=meta_prompt_text,
                pdf_b64=pdf_b64,
                pdf_filename=pdf_filename,
            )
            extracted_errors, extraction_status = ArxivVerifierBaseline._parse_errors(meta_response)
            extracted_locations = [e.get("location", "") for e in extracted_errors]
            meta_dict = {
                "response": meta_response,
                "usage": meta_usage,
                "extracted_errors": extracted_errors,
                "extracted_locations": extracted_locations,
                "extraction_status": extraction_status,
                "num_errors_reviewed": len(error_parts),
            }

        return {
            "rewritten_paper": rewritten_paper,
            "rewrite_usage": rewrite_usage,
            "decomposition": decomposition,
            "parse_check": parse_check,
            "component_verifications": component_results,
            "meta_verification": meta_dict,
            "extracted_errors": extracted_errors,
            "extracted_locations": extracted_locations,
            "extraction_status": extraction_status,
            "block_score": block_score,
        }

    # ── Component verification (multi-vote, DEPS-augmented) ────────
    def _build_block_inputs(
        self, decomposition: Dict[str, Any]
    ) -> List[Tuple[str, str, str, Optional[str], List[str], List[str]]]:
        """Enumerate every verifiable block as
        (key, label, statement, proof, ctx_parts, est_parts).
        Augments est with DEPS-cited blocks (statement-only).
        """
        sorted_theorems = sorted(decomposition.get("theorems", {}).items())
        sorted_props = sorted(decomposition.get("propositions", {}).items())
        deps_map: Dict[str, List[str]] = decomposition.get("deps", {})

        all_theorem_ctx = [
            f"Theorem {tid}: {thm['statement']}" for tid, thm in sorted_theorems
        ]

        def _augment(est: List[str], deps_id: str) -> None:
            seen = set(est)
            for cited in deps_map.get(deps_id, []):
                formatted = _format_block_for_established_arxiv(decomposition, cited)
                if formatted and formatted not in seen:
                    est.append(formatted)
                    seen.add(formatted)

        blocks: List[Tuple[str, str, str, Optional[str], List[str], List[str]]] = []

        # Facts
        for p_num, prop in sorted_props:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    for f_num, fact in sorted(claim.get("facts", {}).items()):
                        ctx = list(all_theorem_ctx) + [
                            f"Proposition {p_num}: {prop['statement']}",
                            f"Lemma {p_num}.{l_num}: {lemma['statement']}",
                            f"Claim {p_num}.{l_num}.{c_num}: {claim['statement']}",
                        ]
                        est: List[str] = []
                        for prev_p, prev_prop in sorted_props:
                            if prev_p < p_num:
                                est.append(f"Proposition {prev_p}: {prev_prop['statement']}")
                        for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                            if prev_l < l_num:
                                est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                        for prev_c, prev_claim in sorted(lemma.get("claims", {}).items()):
                            if prev_c < c_num:
                                est.append(f"Claim {p_num}.{l_num}.{prev_c}: {prev_claim['statement']}")
                        for prev_f, prev_fact in sorted(claim.get("facts", {}).items()):
                            if prev_f < f_num:
                                est.append(f"Fact {p_num}.{l_num}.{c_num}.{prev_f}: {prev_fact['statement']}")
                        _augment(est, f"{p_num}.{l_num}.{c_num}.{f_num}")
                        blocks.append((
                            f"fact_{p_num}_{l_num}_{c_num}_{f_num}",
                            f"Fact {p_num}.{l_num}.{c_num}.{f_num}",
                            fact["statement"], fact["proof"], ctx, est,
                        ))

        # Claims
        for p_num, prop in sorted_props:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    ctx = list(all_theorem_ctx) + [
                        f"Proposition {p_num}: {prop['statement']}",
                        f"Lemma {p_num}.{l_num}: {lemma['statement']}",
                    ]
                    est = []
                    for prev_p, prev_prop in sorted_props:
                        if prev_p < p_num:
                            est.append(f"Proposition {prev_p}: {prev_prop['statement']}")
                    for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                        if prev_l < l_num:
                            est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                    for prev_c, prev_claim in sorted(lemma.get("claims", {}).items()):
                        if prev_c < c_num:
                            est.append(f"Claim {p_num}.{l_num}.{prev_c}: {prev_claim['statement']}")
                    for f_num, fact in sorted(claim.get("facts", {}).items()):
                        est.append(f"Fact {p_num}.{l_num}.{c_num}.{f_num}: {fact['statement']}")
                    _augment(est, f"{p_num}.{l_num}.{c_num}")
                    blocks.append((
                        f"claim_{p_num}_{l_num}_{c_num}",
                        f"Claim {p_num}.{l_num}.{c_num}",
                        claim["statement"], claim["proof"], ctx, est,
                    ))

        # Lemmas
        for p_num, prop in sorted_props:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                ctx = list(all_theorem_ctx) + [f"Proposition {p_num}: {prop['statement']}"]
                est = []
                for prev_p, prev_prop in sorted_props:
                    if prev_p < p_num:
                        est.append(f"Proposition {prev_p}: {prev_prop['statement']}")
                for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                    if prev_l < l_num:
                        est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    est.append(f"Claim {p_num}.{l_num}.{c_num}: {claim['statement']}")
                _augment(est, f"{p_num}.{l_num}")
                blocks.append((
                    f"lemma_{p_num}_{l_num}",
                    f"Lemma {p_num}.{l_num}",
                    lemma["statement"], lemma["proof"], ctx, est,
                ))

        # Propositions
        for p_num, prop in sorted_props:
            ctx = list(all_theorem_ctx)
            est = []
            for prev_p, prev_prop in sorted_props:
                if prev_p < p_num:
                    est.append(f"Proposition {prev_p}: {prev_prop['statement']}")
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                est.append(f"Lemma {p_num}.{l_num}: {lemma['statement']}")
            _augment(est, f"{p_num}")
            blocks.append((
                f"proposition_{p_num}",
                f"Proposition {p_num}",
                prop["statement"], prop["proof"], ctx, est,
            ))

        # Theorems
        for tid, thm in sorted_theorems:
            ctx: List[str] = []
            est = [f"Proposition {p_num}: {prop['statement']}" for p_num, prop in sorted_props]
            for prev_tid, prev_thm in sorted_theorems:
                if prev_tid < tid:
                    est.append(f"Theorem {prev_tid}: {prev_thm['statement']}")
            _augment(est, f"theorem_{tid}")
            blocks.append((
                f"theorem_{tid}",
                f"Theorem {tid}",
                thm["statement"], thm["proof"], ctx, est,
            ))

        return blocks

    async def _verify_components(self, decomposition: Dict[str, Any]) -> Dict[str, Any]:
        blocks = self._build_block_inputs(decomposition)
        tasks = []
        for (key, label, statement, proof, ctx, est) in blocks:
            tasks.append((key, self._verify_component_multi(label, statement, proof, ctx, est)))
        keys = [k for k, _ in tasks]
        results = await asyncio.gather(*(coro for _, coro in tasks))
        return dict(zip(keys, results))

    async def _verify_component_multi(
        self, label, statement, proof, context_parts, established_parts
    ) -> Dict[str, Any]:
        """Run ``self.n_verifications`` verifications of a block in parallel
        and aggregate via majority vote. Returns ``{output, usage, score, runs}``.
        """
        m = max(1, int(self.n_verifications))
        if m == 1:
            r = await self._run_single_verification(label, statement, proof, context_parts, established_parts)
            return {**r, "runs": [r]}
        runs = await asyncio.gather(*[
            self._run_single_verification(label, statement, proof, context_parts, established_parts)
            for _ in range(m)
        ])
        scores = [r["score"] for r in runs if r.get("score") is not None]
        if not scores:
            agg = None
        else:
            correct = sum(1 for s in scores if s >= 7)
            agg = 7 if correct * 2 > len(scores) else 0
        rep = next((r for r in runs if r.get("score") == agg), runs[0])
        return {"output": rep.get("output"), "usage": rep.get("usage"), "score": agg, "runs": runs}

    async def _run_single_verification(
        self, label: str, statement: str, proof: Optional[str],
        context_parts: List[str], established_parts: List[str],
    ) -> Dict[str, Any]:
        ctx = "\n\n".join(context_parts) if context_parts else "None"
        est = "\n\n".join(established_parts) if established_parts else "None"
        prompt = ARXIV_COMPLEX_COMPONENT_VERIFY_PROMPT.format(
            contexts=ctx,
            assertion=f"{label}: {statement}",
            established_results=est,
            proof=proof,
        )
        output, usage = await self._completion_text_only(prompt)
        verdict, combined = self._parse_verdict(output)
        return {
            "output": combined,
            "llm_output": output,
            "usage": usage,
            "score": 7 if verdict == "CORRECT" else 0,
        }

    # ── Faithfulness check (text-only, paper-aware) ────────────────
    async def _check_faithfulness_arxiv(
        self, original_paper: str, decomposition: Dict[str, Any]
    ) -> Dict[str, Any]:
        async def _faith(label, statement, proof, ctx_parts, est_parts):
            ctx = "\n\n".join(ctx_parts) if ctx_parts else "None"
            est = "\n\n".join(est_parts) if est_parts else "None"
            prompt = ARXIV_COMPONENT_FAITHFULNESS_PROMPT.format(
                original_paper=original_paper,
                contexts=ctx,
                established_results=est,
                assertion=f"{label}: {statement}",
                proof=proof or "",
            )
            output, usage = await self._completion_text_only(prompt)
            verdict, error_desc = _parse_faithfulness_verdict(output)
            return {"verdict": verdict, "error_description": error_desc, "usage": usage}

        # Reuse the same context/established assembly as block verification.
        blocks = self._build_block_inputs(decomposition)
        tasks = [
            (key, _faith(label, statement, proof, ctx, est))
            for (key, label, statement, proof, ctx, est) in blocks
        ]
        keys = [k for k, _ in tasks]
        results = await asyncio.gather(*(coro for _, coro in tasks))
        return dict(zip(keys, results))

    # ── Global block check + reverify (text-only) ──────────────────
    async def _run_global_block_check(
        self, block_label: str, full_proof: str, tags_by_id: Dict[str, Dict[str, str]],
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        formatted_items: List[str] = []
        for tag_id, info in tags_by_id.items():
            formatted_items.append(
                f"<{info['type']} id=\"{tag_id}\">\n{info['content']}\n</{info['type']}>"
            )
        prompt = GLOBAL_BLOCK_CHECK_PROMPT.format(
            full_proof=full_proof or "(rewritten proof unavailable)",
            block_id=block_label,
            flagged_items="\n\n".join(formatted_items),
        )
        output, usage = await self._completion_text_only(prompt)
        parsed = _parse_global_check_json(output)

        results: Dict[str, Dict[str, Any]] = {}
        for tag_id in tags_by_id:
            entry = parsed.get(tag_id)
            if not isinstance(entry, dict):
                results[tag_id] = {"Addressed by": []}
                continue
            raw = entry.get("Addressed by", [])
            addressed: List[Dict[str, str]] = []
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    bid = str(item.get("id", "")).strip()
                    if not bid:
                        continue
                    parts = str(item.get("parts", "statement")).strip().lower()
                    if parts not in ("statement", "both"):
                        parts = "statement"
                    addressed.append({"id": bid, "parts": parts})
            results[tag_id] = {"Addressed by": addressed}
        return results, usage

    async def _apply_global_block_check(
        self,
        component_results: Dict[str, Any],
        rewritten_proof: Optional[str],
        run_indices_per_block: Dict[str, List[int]],
    ) -> None:
        tasks: List[Tuple[str, Any]] = []
        for key, rid_list in run_indices_per_block.items():
            entry = component_results.get(key)
            if not isinstance(entry, dict):
                continue
            runs = entry.get("runs") or []
            merged: Dict[str, Dict[str, str]] = {}
            for rid in rid_list:
                if 1 <= rid <= len(runs):
                    merged.update(_extract_missing_tags(runs[rid - 1].get("output"), rid))
            if not merged:
                continue
            tasks.append((key, self._run_global_block_check(
                _block_key_to_label(key), rewritten_proof or "", merged,
            )))

        if not tasks:
            return

        keys = [k for k, _ in tasks]
        results = await asyncio.gather(*(coro for _, coro in tasks))
        for key, (per_tag, usage) in zip(keys, results):
            entry = component_results[key]
            runs = entry.get("runs") or []
            per_run: Dict[int, Dict[str, Dict[str, str]]] = {}
            for tag_id, val in per_tag.items():
                m = re.match(r"missing_(?:lemma|definition)_(\d+)\.\d+$", tag_id)
                if not m:
                    continue
                per_run.setdefault(int(m.group(1)), {})[tag_id] = val
            for rid in run_indices_per_block.get(key, []):
                if 1 <= rid <= len(runs):
                    runs[rid - 1]["global_check"] = per_run.get(rid, {})
            entry["global_check_usage"] = usage

    async def _apply_global_check_reverification(
        self, component_results: Dict[str, Any], decomposition: Dict[str, Any],
    ) -> None:
        if not decomposition or not component_results:
            return

        m_re = max(1, int(self.m_re))
        blocks_by_key = {b[0]: b for b in self._build_block_inputs(decomposition)}

        reverify_tasks: List[Tuple[str, Any]] = []
        added_est_by_key: Dict[str, List[str]] = {}

        for key, entry in component_results.items():
            if not isinstance(entry, dict):
                continue
            runs = entry.get("runs") or []
            by_id: Dict[str, str] = {}
            for run in runs:
                gc = run.get("global_check") or {}
                for tag_info in gc.values():
                    if not isinstance(tag_info, dict):
                        continue
                    for item in tag_info.get("Addressed by") or []:
                        if not isinstance(item, dict):
                            continue
                        bid = str(item.get("id", "")).strip()
                        bparts = str(item.get("parts", "statement")).strip().lower()
                        if bparts not in ("statement", "both"):
                            bparts = "statement"
                        if not bid:
                            continue
                        existing = by_id.get(bid)
                        if existing is None or (existing == "statement" and bparts == "both"):
                            by_id[bid] = bparts
            if not by_id:
                continue

            # Same citation legality enforcement as the block-verifier rules:
            # cannot self-cite, cannot cite ancestors, cannot cite later-subtree
            # non-descendants. Special handling for 'theorem_N' citing keys.
            self_dotted = _block_key_to_dotted_id(key)
            self_thm = _parse_theorem_ref(key)
            if self_thm is not None:
                # Theorem block: filter cited ids — drop later/equal theorems.
                for bid in list(by_id):
                    cited_thm = _parse_theorem_ref(bid)
                    if cited_thm is not None and cited_thm >= self_thm:
                        del by_id[bid]
                # Theorem can cite any dotted id (proposition pool); leave dotted ids alone.
            elif self_dotted:
                try:
                    self_tuple = tuple(_parse_dotted_id(self_dotted) or [])
                except (ValueError, TypeError):
                    self_tuple = ()

                def _strict_prefix(prefix, full):
                    return len(prefix) < len(full) and full[: len(prefix)] == prefix

                for bid in list(by_id):
                    s = bid.strip()
                    if _parse_theorem_ref(s) is not None:
                        del by_id[bid]; continue
                    try:
                        cited_tuple = tuple(_parse_dotted_id(s) or [])
                    except (ValueError, TypeError):
                        del by_id[bid]; continue
                    if not cited_tuple or cited_tuple == self_tuple:
                        del by_id[bid]; continue
                    if _strict_prefix(cited_tuple, self_tuple):
                        del by_id[bid]; continue
                    if cited_tuple > self_tuple and not _strict_prefix(self_tuple, cited_tuple):
                        del by_id[bid]
            if not by_id:
                continue

            base = blocks_by_key.get(key)
            if base is None:
                continue
            _, label, statement, proof, ctx, est = base
            existing_est_set = set(est)
            added_est: List[str] = []
            for bid, bparts in by_id.items():
                formatted = _format_block_for_established_arxiv(
                    decomposition, bid, include_proof=(bparts == "both")
                )
                if formatted and formatted not in existing_est_set:
                    added_est.append(formatted)
                    existing_est_set.add(formatted)
            if not added_est:
                continue
            augmented_est = list(est) + added_est
            added_est_by_key[key] = added_est
            for _ in range(m_re):
                reverify_tasks.append((key, self._run_single_verification(
                    label, statement, proof, ctx, augmented_est,
                )))

        if not reverify_tasks:
            return

        results = await asyncio.gather(*(coro for _, coro in reverify_tasks))
        new_runs_by_key: Dict[str, List[Dict[str, Any]]] = {}
        for (k, _), r in zip(reverify_tasks, results):
            new_runs_by_key.setdefault(k, []).append(r)

        for key, new_runs in new_runs_by_key.items():
            scores = [r["score"] for r in new_runs if r.get("score") is not None]
            if not scores:
                agg = None
            else:
                correct = sum(1 for s in scores if s >= 7)
                agg = 7 if correct * 2 > len(scores) else 0
            entry = component_results[key]
            entry["reverification_runs"] = new_runs
            entry["reverification_score"] = agg
            entry["reverification_added_established_results"] = added_est_by_key[key]
            if agg is not None:
                rep = next((r for r in new_runs if r.get("score") == agg), new_runs[0])
                entry["output"] = rep.get("output")
                entry["usage"] = rep.get("usage")
                entry["score"] = agg

    # ── topup_verifications (text-only) ────────────────────────────
    async def topup_verifications(
        self, entry: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, int]]:
        """Top up each block's verification runs to ``self.n_verifications``.
        Identical mechanism to ``ComplexPseudoFormalisationVerifier.topup_verifications``
        but with text-only completion and the multi-theorem block enumeration.
        """
        stats = {"added": 0, "blocks_touched": 0, "min_existing": 0, "max_existing": 0}
        decomposition = entry.get("decomposition")
        if not decomposition:
            return entry, stats

        m = max(1, int(self.n_verifications))
        component_results: Dict[str, Any] = dict(entry.get("component_verifications") or {})
        blocks = self._build_block_inputs(decomposition)

        existing_runs_per_block: Dict[str, List[Dict[str, Any]]] = {}
        existing_counts: List[int] = []
        for key, *_ in blocks:
            cur = component_results.get(key)
            if cur is None:
                existing_runs_per_block[key] = []
                existing_counts.append(0)
                continue
            if isinstance(cur.get("runs"), list):
                existing_runs_per_block[key] = list(cur["runs"])
            elif cur.get("score") is not None:
                existing_runs_per_block[key] = [{
                    "output": cur.get("output"), "usage": cur.get("usage"), "score": cur.get("score"),
                }]
            else:
                existing_runs_per_block[key] = []
            existing_counts.append(len(existing_runs_per_block[key]))

        if existing_counts:
            stats["min_existing"] = min(existing_counts)
            stats["max_existing"] = max(existing_counts)

        topup_tasks: List[Tuple[str, Any]] = []
        for (key, label, statement, proof, ctx, est) in blocks:
            needed = m - len(existing_runs_per_block.get(key, []))
            if needed > 0:
                stats["blocks_touched"] += 1
                for _ in range(needed):
                    topup_tasks.append((key, self._run_single_verification(label, statement, proof, ctx, est)))

        if not topup_tasks:
            return entry, stats

        stats["added"] = len(topup_tasks)
        results = await asyncio.gather(*(coro for _, coro in topup_tasks))
        new_by_key: Dict[str, List[Dict[str, Any]]] = {}
        for (k, _), r in zip(topup_tasks, results):
            new_by_key.setdefault(k, []).append(r)

        for key, new_runs in new_by_key.items():
            all_runs = existing_runs_per_block.get(key, []) + new_runs
            scores = [r["score"] for r in all_runs if r.get("score") is not None]
            if not scores:
                agg = None
            else:
                correct = sum(1 for s in scores if s >= 7)
                agg = 7 if correct * 2 > len(scores) else 0
            rep = next((r for r in all_runs if r.get("score") == agg), all_runs[0])
            component_results[key] = {
                "output": rep.get("output"),
                "usage": rep.get("usage"),
                "score": agg,
                "runs": all_runs,
            }

        if self.global_block_check:
            new_run_indices: Dict[str, List[int]] = {}
            for key, new_runs in new_by_key.items():
                if component_results.get(key, {}).get("score") == 7:
                    continue
                existing_len = len(existing_runs_per_block.get(key, []))
                new_run_indices[key] = list(range(existing_len + 1, existing_len + len(new_runs) + 1))
            if new_run_indices:
                await self._apply_global_block_check(
                    component_results, entry.get("rewritten_paper"), new_run_indices,
                )
                await self._apply_global_check_reverification(component_results, decomposition)

        entry["component_verifications"] = component_results
        all_scores = [r["score"] for r in component_results.values() if r.get("score") is not None]
        block_score = min(all_scores) if all_scores else 0
        entry["block_score"] = block_score
        return entry, stats

    # ── Verdict parser (no <web_search> block) ─────────────────────
    @staticmethod
    def _parse_verdict(output: str) -> Tuple[str, Optional[str]]:
        """Parse the tagged output of ARXIV_COMPLEX_COMPONENT_VERIFY_PROMPT.
        Returns (verdict, combined_text) where combined is a readable summary
        of error_description / gap_filling / cited_result_audits.
        """
        def _tag(name: str, text: str) -> Optional[str]:
            m = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
            if not m:
                return None
            val = m.group(1).strip()
            return val if val else None

        def _all_tags(name: str, text: str) -> List[str]:
            return [m.group(1) for m in re.finditer(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)]

        verdict = None
        v_raw = _tag("verdict", output)
        if v_raw:
            v = v_raw.upper()
            if v in ("CORRECT", "INCORRECT"):
                verdict = v

        if verdict is None:
            m = re.search(r"\b(CORRECT|INCORRECT)\b", output)
            if m:
                verdict = m.group(1)

        if verdict is None:
            verdict = "INCORRECT"

        error_desc = _tag("error_description", output)
        gap_filling = _tag("gap_filling", output)

        audits_blob = _tag("cited_result_audits", output)
        cited_result_audits: List[Dict[str, Any]] = []
        if audits_blob:
            for audit_text in _all_tags("audit", audits_blob):
                hypotheses: List[Dict[str, Any]] = []
                for hyp_text in _all_tags("hypothesis", audit_text):
                    hypotheses.append({
                        "hypothesis": _tag("statement", hyp_text),
                        "satisfied": _tag("satisfied", hyp_text),
                        "justification": _tag("justification", hyp_text),
                    })
                cited_result_audits.append({
                    "cited_as": _tag("cited_as", audit_text),
                    "hypotheses": hypotheses,
                })

        # Combined readable summary (stored in the block's "output" field).
        parts: List[str] = []
        if verdict == "INCORRECT":
            if error_desc:
                parts.append(f"ERROR: {error_desc}")
            else:
                parts.append(f"ERROR (no description parsed); raw output:\n{output}")
        if gap_filling:
            parts.append(f"GAP_FILLING: {gap_filling}")
        if cited_result_audits:
            parts.append(f"AUDITS: {json.dumps(cited_result_audits, ensure_ascii=False)}")
        combined = "\n\n".join(parts) if parts else None

        return verdict, combined

    # ── Completion helpers ─────────────────────────────────────────
    async def _completion_pdf_text(
        self, prompt_text: str, pdf_b64: str, pdf_filename: str, system_prompt: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        tokens_used: Dict[str, Any] = {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
            "final_output_tokens": 0,
        }
        content_blocks = [
            {
                "type": "input_file",
                "filename": pdf_filename,
                "file_data": f"data:application/pdf;base64,{pdf_b64}",
            },
            {"type": "input_text", "text": prompt_text},
        ]
        for attempt in range(self.max_tries):
            try:
                start = time.perf_counter()
                resp = await self.client.responses.create(
                    model=self.model,
                    instructions=system_prompt or None,
                    input=[{"role": "user", "content": content_blocks}],
                    text={"format": {"type": "text"}},
                    max_output_tokens=self.max_tokens,
                    reasoning={"effort": self.effort},
                )
                duration = time.perf_counter() - start
                usage = getattr(resp, "usage", None)
                if usage:
                    tokens_used = _usage_to_dict(usage)
                tokens_used["time"] = duration
                return (getattr(resp, "output_text", "") or ""), tokens_used
            except Exception as e:
                msg = str(e).lower()
                retryable = any(s in msg for s in ("rate", "429", "timeout", "timed out", "temporarily", "overloaded", " 5"))
                if attempt == self.max_tries - 1 or not retryable:
                    return "", tokens_used
                await asyncio.sleep(self.base * (2 ** attempt))

    async def _completion_text_only(
        self, prompt: str, system_prompt: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        tokens_used: Dict[str, Any] = {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
            "final_output_tokens": 0,
        }
        for attempt in range(self.max_tries):
            try:
                start = time.perf_counter()
                resp = await self.client.responses.create(
                    model=self.model,
                    instructions=system_prompt or None,
                    input=[{"role": "user", "content": prompt}],
                    text={"format": {"type": "text"}},
                    max_output_tokens=self.max_tokens,
                    reasoning={"effort": self.effort},
                )
                duration = time.perf_counter() - start
                usage = getattr(resp, "usage", None)
                if usage:
                    tokens_used = _usage_to_dict(usage)
                tokens_used["time"] = duration
                return (getattr(resp, "output_text", "") or ""), tokens_used
            except Exception as e:
                msg = str(e).lower()
                retryable = any(s in msg for s in ("rate", "429", "timeout", "timed out", "temporarily", "overloaded", " 5"))
                if attempt == self.max_tries - 1 or not retryable:
                    return "", tokens_used
                await asyncio.sleep(self.base * (2 ** attempt))
