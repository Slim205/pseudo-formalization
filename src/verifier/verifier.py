from src.verifier.prompts import (
    WITHOUT_REFERENCE_PROMPT,
    REWRITE_PROMPT,
    REGENERATE_REWRITE_PROMPT,
    COMPONENT_VERIFY_PROMPT,
    META_VERIFY_PROMPT,
    HARD2VERIFY_STEP_META_VERIFY_PROMPT,
    COMPONENT_FAITHFULNESS_PROMPT,
    ARXIV_REFEREE_PROMPT,
    ARXIV_REWRITE_PROMPT,
    ARXIV_REGENERATE_REWRITE_PROMPT,
    ARXIV_COMPONENT_FAITHFULNESS_PROMPT,
    ARXIV_META_VERIFY_PROMPT,
    ARXIV_REFEREE_PROMPT_PDF_ONLY,
    ARXIV_REWRITE_PROMPT_PDF_ONLY,
    ARXIV_REGENERATE_REWRITE_PROMPT_PDF_ONLY,
    ARXIV_COMPONENT_FAITHFULNESS_PROMPT_PDF_ONLY,
    ARXIV_META_VERIFY_PROMPT_PDF_ONLY,
)
import os
import asyncio
import re
import sys
import time
import json
from typing import Any, Dict, Tuple, Optional, List

from openai import AsyncOpenAI
from src.verifier import Verifier
from src.utils import parse_score


def _get(obj: Any, name: str, default=None):
    if obj is None:
        return default
    if hasattr(obj, name):
        val = getattr(obj, name)
        return default if val is None else val
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def _usage_to_dict(usage: Any) -> Dict[str, Any]:
    itd = _get(usage, "input_tokens_details")
    otd = _get(usage, "output_tokens_details")

    data: Dict[str, Any] = {
        "input_tokens": _get(usage, "input_tokens", 0) or 0,
        "output_tokens": _get(usage, "output_tokens", 0) or 0,
        "total_tokens": _get(usage, "total_tokens", 0) or 0,
        "output_tokens_details": {
            "reasoning_tokens": _get(otd, "reasoning_tokens", 0) or 0,
        },
    }
    if not data["total_tokens"]:
        data["total_tokens"] = data["input_tokens"] + data["output_tokens"]
    # "Visible" tokens shown to users (output minus reasoning trace)
    data["final_output_tokens"] = max(
        0, data["output_tokens"] - data["output_tokens_details"]["reasoning_tokens"]
    )
    return data


FIRST_INCORRECT_STEP_RE = re.compile(
    r"<first_incorrect_step>\s*(-?\d+)\s*</first_incorrect_step>",
    re.IGNORECASE | re.DOTALL,
)
STEP_VERDICTS_RE = re.compile(
    r"<step_verdicts>\s*(.*?)\s*</step_verdicts>",
    re.IGNORECASE | re.DOTALL,
)


def _indexed_solution_steps(steps: List[Any]) -> str:
    return "\n".join(f"<step>[{i}] {step}</step>" for i, step in enumerate(steps))


def _parse_step_meta_verification(text: str, num_steps: int) -> Dict[str, Any]:
    first_match = FIRST_INCORRECT_STEP_RE.search(text or "")
    first_step = int(first_match.group(1)) if first_match else None

    verdicts_match = STEP_VERDICTS_RE.search(text or "")
    raw_verdicts = verdicts_match.group(1).strip() if verdicts_match else None
    verdicts = None
    if raw_verdicts is not None:
        verdicts = [
            part.strip().lower()
            for part in raw_verdicts.split(",")
            if part.strip()
        ]

    verdicts_valid = (
        verdicts is not None
        and len(verdicts) == num_steps
        and all(v in {"yes", "no"} for v in verdicts)
    )
    expected_first = None
    if verdicts_valid:
        expected_first = next(
            (i for i, verdict in enumerate(verdicts) if verdict == "no"),
            -1,
        )

    first_valid = first_step is not None and -1 <= first_step < num_steps
    consistent = verdicts_valid and first_valid and first_step == expected_first
    return {
        "first_incorrect_step": first_step,
        "step_verdicts": verdicts,
        "raw_step_verdicts": raw_verdicts,
        "expected_first_from_verdicts": expected_first,
        "num_step_verdicts": len(verdicts) if verdicts is not None else None,
        "valid": bool(consistent),
        "parse_errors": [
            msg
            for msg, ok in (
                ("missing_step_verdicts", verdicts is not None),
                ("wrong_number_or_value_of_step_verdicts", verdicts_valid),
                ("missing_first_incorrect_step", first_step is not None),
                ("first_incorrect_step_out_of_range", first_valid),
                ("first_incorrect_step_not_consistent_with_step_verdicts", consistent),
            )
            if not ok
        ],
    }


class Verfifierbaseline(Verifier):

    def __init__(
        self,
        n: int = 1,
        max_tries=3,
        base=10,
        model: str = "gpt-5.4-mini-2026-03-17",
        effort: str = "high",
        max_output_tokens: int = 50000,
    ):
        self.client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.base = base
        self.max_tries = max_tries
        self.n = n
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.effort = effort

    async def process_row(self, row):
        """Calls LLM, returns (full_text, parsed_score)."""

        prompt = WITHOUT_REFERENCE_PROMPT.format(
            problem_statement=row["Problem"],
            student_answer=row["Response"],
        )
        return await self.async_completion(prompt)

    async def async_completion(
        self,
        prompt: str,
        system_prompt: str = "",
    ) -> Tuple[str, Dict[str, int]]:
        tokens_used = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        for attempt in range(self.max_tries):
            try:
                start = time.perf_counter()
                resp = await self.client.responses.create(
                    model=self.model,
                    instructions=system_prompt or None,
                    input=[{"role": "user", "content": prompt}],
                    text={"format": {"type": "text"}},
                    max_output_tokens=self.max_output_tokens,
                    reasoning={"effort": self.effort},
                )
                elapsed = time.perf_counter() - start

                usage = getattr(resp, "usage", None)
                if usage:
                    it = getattr(usage, "input_tokens", 0) or 0
                    ot = getattr(usage, "output_tokens", 0) or 0
                    tt = getattr(usage, "total_tokens", None)
                    tokens_used.update(
                        {
                            "input_tokens": it,
                            "output_tokens": ot,
                            "total_tokens": tt if isinstance(tt, int) else it + ot,
                        }
                    )

                return (resp.output_text or ""), tokens_used
            except Exception as e:
                msg = str(e).lower()
                retryable = (
                    "rate" in msg
                    or "429" in msg
                    or "timeout" in msg
                    or "temporarily" in msg
                    or "overloaded" in msg
                    or " 5" in msg
                )
                print(
                    f"!! API error (attempt {attempt + 1}/{self.max_tries}): "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                if attempt == self.max_tries - 1 or not retryable:
                    return "", tokens_used
                await asyncio.sleep(self.base * (2**attempt))


def _extract_tag(text: str, tag: str, id_val: Optional[str] = None) -> Optional[str]:
    """Extract the content of a single XML-style tag from *text*.

    If *id_val* is given, matches ``<TAG id="id_val">...</TAG>``.
    Otherwise matches ``<TAG>...</TAG>`` (no id attribute).
    Returns the stripped inner content, or ``None`` if not found.
    """
    if id_val is not None:
        pattern = rf"<{tag}\s+id\s*=\s*\"{re.escape(id_val)}\"\s*>(.*?)</{tag}>"
    else:
        pattern = rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else None


def _extract_all_ids(text: str, tag: str) -> List[str]:
    """Return all id values for a given tag name, in order of appearance."""
    pattern = rf"<{tag}\s+id\s*=\s*\"([^\"]+)\"\s*>"
    return re.findall(pattern, text)


def _parse_dotted_id(id_str: str) -> Optional[List[int]]:
    """Parse a dotted id string like '1.2.3' into [1, 2, 3].

    Returns ``None`` if any part is not an integer — callers should skip
    ids they cannot parse so the structural-completeness check picks up
    the missing component and triggers a rewrite retry.
    """
    try:
        return [int(x) for x in id_str.split(".")]
    except ValueError:
        return None


def _ensure_parent_chain(propositions: Dict[int, Dict[str, Any]], parts: List[int]):
    """Ensure all ancestor nodes exist in the tree for a given dotted id."""
    # Ensure proposition
    pid = parts[0]
    if pid not in propositions:
        propositions[pid] = {"statement": "", "proof": None, "lemmas": {}}
    if len(parts) < 2:
        return
    # Ensure lemma
    lid = parts[1]
    if lid not in propositions[pid]["lemmas"]:
        propositions[pid]["lemmas"][lid] = {
            "statement": "",
            "proof": None,
            "claims": {},
        }
    if len(parts) < 3:
        return
    # Ensure claim
    cid = parts[2]
    if cid not in propositions[pid]["lemmas"][lid]["claims"]:
        propositions[pid]["lemmas"][lid]["claims"][cid] = {
            "statement": "",
            "proof": None,
            "facts": {},
        }


def parse_rewritten_proof(text: str) -> Dict[str, Any]:
    """Parse a rewritten proof (with XML delimiter tags) into structured components.

    Supports up to 4 layers: Theorem -> Proposition -> Lemma -> Claim -> Fact.

    Returns:
        {
            "preamble": str,
            "theorem": {"statement": str, "proof": str},
            "propositions": {
                int: {
                    "statement": str,
                    "proof": str,
                    "lemmas": {
                        int: {
                            "statement": str,
                            "proof": str,
                            "claims": {
                                int: {
                                    "statement": str,
                                    "proof": str,
                                    "facts": {
                                        int: {"statement": str, "proof": str},
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "_raw_sections": list[str],
        }
    """
    theorem_statement = _extract_tag(text, "THEOREM_STATEMENT") or ""
    theorem_proof = _extract_tag(text, "THEOREM_PROOF")

    # ── Propositions (top-level, integer ids) ──
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

    # ── Lemmas (2-level dotted ids: "1.1", "2.3") ──
    lemma_ids = _extract_all_ids(text, "LEMMA_STATEMENT")
    for lid_str in lemma_ids:
        parts = _parse_dotted_id(lid_str)
        if parts is None or not parts:
            continue
        pid = parts[0]
        lid = parts[1] if len(parts) > 1 else 1
        _ensure_parent_chain(propositions, [pid])
        propositions[pid]["lemmas"][lid] = {
            "statement": _extract_tag(text, "LEMMA_STATEMENT", lid_str) or "",
            "proof": _extract_tag(text, "LEMMA_PROOF", lid_str),
            "claims": {},
        }

    # ── Claims (3-level dotted ids: "1.1.1", "2.3.2") ──
    claim_ids = _extract_all_ids(text, "CLAIM_STATEMENT")
    for cid_str in claim_ids:
        parts = _parse_dotted_id(cid_str)
        if parts is None or not parts:
            continue
        pid, lid = parts[0], parts[1] if len(parts) > 1 else 1
        cid = parts[2] if len(parts) > 2 else 1
        _ensure_parent_chain(propositions, [pid, lid])
        propositions[pid]["lemmas"][lid]["claims"][cid] = {
            "statement": _extract_tag(text, "CLAIM_STATEMENT", cid_str) or "",
            "proof": _extract_tag(text, "CLAIM_PROOF", cid_str),
            "facts": {},
        }

    # ── Facts (4-level dotted ids: "1.1.1.1", "2.3.2.1") ──
    fact_ids = _extract_all_ids(text, "FACT_STATEMENT")
    for fid_str in fact_ids:
        parts = _parse_dotted_id(fid_str)
        if parts is None or not parts:
            continue
        pid, lid = parts[0], parts[1] if len(parts) > 1 else 1
        cid = parts[2] if len(parts) > 2 else 1
        fid = parts[3] if len(parts) > 3 else 1
        _ensure_parent_chain(propositions, [pid, lid, cid])
        propositions[pid]["lemmas"][lid]["claims"][cid]["facts"][fid] = {
            "statement": _extract_tag(text, "FACT_STATEMENT", fid_str) or "",
            "proof": _extract_tag(text, "FACT_PROOF", fid_str),
        }

    # ── Raw sections (all tag contents in document order) ──
    all_tags = "|".join(
        [
            "THEOREM_STATEMENT",
            "PROPOSITION_STATEMENT",
            "LEMMA_STATEMENT",
            "CLAIM_STATEMENT",
            "FACT_STATEMENT",
            "FACT_PROOF",
            "CLAIM_PROOF",
            "LEMMA_PROOF",
            "PROPOSITION_PROOF",
            "THEOREM_PROOF",
        ]
    )
    raw_pattern = rf"<({all_tags})(?:\s[^>]*)?>(.+?)</\1>"
    raw_sections = [
        m.group(2).strip() for m in re.finditer(raw_pattern, text, re.DOTALL)
    ]

    return {
        "preamble": "",
        "theorem": {
            "statement": theorem_statement,
            "proof": theorem_proof,
        },
        "propositions": propositions,
        "_raw_sections": raw_sections,
    }


def _extract_tag_order(text: str) -> List[Tuple[str, Optional[str]]]:
    """Extract the order of XML tags from the original text.

    Returns a list of (tag_name, id_or_None) in the order they appear.
    """
    all_tags = "|".join(
        [
            "THEOREM_STATEMENT",
            "PROPOSITION_STATEMENT",
            "LEMMA_STATEMENT",
            "CLAIM_STATEMENT",
            "FACT_STATEMENT",
            "FACT_PROOF",
            "CLAIM_PROOF",
            "LEMMA_PROOF",
            "PROPOSITION_PROOF",
            "THEOREM_PROOF",
        ]
    )
    pattern = rf"<({all_tags})(?:\s+id=\"([^\"]*)\")?\s*>"
    return [(m.group(1), m.group(2)) for m in re.finditer(pattern, text)]


def _get_content_for_tag(
    decomposition: Dict[str, Any], tag_name: str, tag_id: Optional[str]
) -> Optional[str]:
    """Look up the content for a given tag from the decomposition dict."""
    thm = decomposition["theorem"]

    if tag_name == "THEOREM_STATEMENT":
        return thm["statement"]
    if tag_name == "THEOREM_PROOF":
        return thm["proof"]

    if tag_id is None:
        return None

    parts = _parse_dotted_id(tag_id)
    if parts is None or not parts:
        return None
    props = decomposition["propositions"]
    pid = parts[0]

    if tag_name == "PROPOSITION_STATEMENT":
        return props.get(pid, {}).get("statement")
    if tag_name == "PROPOSITION_PROOF":
        return props.get(pid, {}).get("proof")

    lid = parts[1] if len(parts) > 1 else 1
    lemma = props.get(pid, {}).get("lemmas", {}).get(lid)
    if lemma is None:
        return None

    if tag_name == "LEMMA_STATEMENT":
        return lemma.get("statement")
    if tag_name == "LEMMA_PROOF":
        return lemma.get("proof")

    cid = parts[2] if len(parts) > 2 else 1
    claim = lemma.get("claims", {}).get(cid)
    if claim is None:
        return None

    if tag_name == "CLAIM_STATEMENT":
        return claim.get("statement")
    if tag_name == "CLAIM_PROOF":
        return claim.get("proof")

    fid = parts[3] if len(parts) > 3 else 1
    fact = claim.get("facts", {}).get(fid)
    if fact is None:
        return None

    if tag_name == "FACT_STATEMENT":
        return fact.get("statement")
    if tag_name == "FACT_PROOF":
        return fact.get("proof")

    return None


def _reconstruct_from_decomposition(
    decomposition: Dict[str, Any], original: Optional[str] = None
) -> str:
    """Reconstruct the XML-tagged proof text from a parsed decomposition dict.

    If *original* is provided, tags are emitted in the same order they appear
    in the original text.  Otherwise falls back to sorted-id order.
    """
    if original is not None:
        tag_order = _extract_tag_order(original)
        parts: List[str] = []
        for tag_name, tag_id in tag_order:
            content = _get_content_for_tag(decomposition, tag_name, tag_id)
            if content is None:
                continue
            if tag_id is not None:
                parts.append(f'<{tag_name} id="{tag_id}">\n{content}\n</{tag_name}>')
            else:
                parts.append(f"<{tag_name}>\n{content}\n</{tag_name}>")
        return "\n\n".join(parts)

    # Fallback: sorted order
    parts = []
    thm = decomposition["theorem"]

    parts.append(f"<THEOREM_STATEMENT>\n{thm['statement']}\n</THEOREM_STATEMENT>")

    for pid in sorted(decomposition["propositions"]):
        prop = decomposition["propositions"][pid]
        parts.append(
            f'<PROPOSITION_STATEMENT id="{pid}">\n{prop["statement"]}\n</PROPOSITION_STATEMENT>'
        )

        for lid in sorted(prop.get("lemmas", {})):
            lemma = prop["lemmas"][lid]
            parts.append(
                f'<LEMMA_STATEMENT id="{pid}.{lid}">\n{lemma["statement"]}\n</LEMMA_STATEMENT>'
            )

            for cid in sorted(lemma.get("claims", {})):
                claim = lemma["claims"][cid]
                parts.append(
                    f'<CLAIM_STATEMENT id="{pid}.{lid}.{cid}">\n{claim["statement"]}\n</CLAIM_STATEMENT>'
                )

                for fid in sorted(claim.get("facts", {})):
                    fact = claim["facts"][fid]
                    parts.append(
                        f'<FACT_STATEMENT id="{pid}.{lid}.{cid}.{fid}">\n{fact["statement"]}\n</FACT_STATEMENT>'
                    )
                    if fact["proof"] is not None:
                        parts.append(
                            f'<FACT_PROOF id="{pid}.{lid}.{cid}.{fid}">\n{fact["proof"]}\n</FACT_PROOF>'
                        )

                if claim["proof"] is not None:
                    parts.append(
                        f'<CLAIM_PROOF id="{pid}.{lid}.{cid}">\n{claim["proof"]}\n</CLAIM_PROOF>'
                    )

            if lemma["proof"] is not None:
                parts.append(
                    f'<LEMMA_PROOF id="{pid}.{lid}">\n{lemma["proof"]}\n</LEMMA_PROOF>'
                )

        if prop["proof"] is not None:
            parts.append(
                f'<PROPOSITION_PROOF id="{pid}">\n{prop["proof"]}\n</PROPOSITION_PROOF>'
            )

    if thm["proof"] is not None:
        parts.append(f"<THEOREM_PROOF>\n{thm['proof']}\n</THEOREM_PROOF>")

    return "\n\n".join(parts)


def _normalize_for_comparison(text: str) -> str:
    """Normalize whitespace for comparison: collapse runs of whitespace to single space, strip."""
    return re.sub(r"\s+", " ", text).strip()


# =============================================================================
# Paper-form parser (for arxiv decomposed verifier).
# Identical to parse_rewritten_proof except that <THEOREM_STATEMENT id="N">
# and <THEOREM_PROOF id="N"> may appear multiple times at the top of the
# rewrite, one block per top-level theorem the paper proves. Propositions
# remain a single shared pool numbered globally across all theorems.
# =============================================================================


def parse_rewritten_paper(text: str) -> Dict[str, Any]:
    """Parse a rewritten paper (multi-theorem XML form) into structured components.

    Differs from ``parse_rewritten_proof`` in that the theorem level is a
    dict keyed by integer id (multiple top-level theorems allowed). The
    proposition/lemma/claim/fact tree is shared across all theorems.

    Returns:
        {
            "preamble": str,
            "theorems": {
                int: {"statement": str, "proof": str},
                ...
            },
            "propositions": {
                int: {
                    "statement": str,
                    "proof": str,
                    "lemmas": {
                        int: {
                            "statement": str,
                            "proof": str,
                            "claims": {
                                int: {
                                    "statement": str,
                                    "proof": str,
                                    "facts": {
                                        int: {"statement": str, "proof": str},
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "_raw_sections": list[str],
        }
    """
    # ── Theorems (top-level, integer ids) ─────────────────────────────
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

    # ── Propositions (top-level, integer ids) ─────────────────────────
    prop_ids = _extract_all_ids(text, "PROPOSITION_STATEMENT")
    propositions: Dict[int, Dict[str, Any]] = {}
    for pid_str in prop_ids:
        try:
            pid = int(pid_str)
        except ValueError:
            # Model emitted a non-integer proposition id (e.g. "2.4"). Skip;
            # the structural-completeness check will surface the missing
            # component and trigger a rewrite retry.
            continue
        propositions[pid] = {
            "statement": _extract_tag(text, "PROPOSITION_STATEMENT", pid_str) or "",
            "proof": _extract_tag(text, "PROPOSITION_PROOF", pid_str),
            "lemmas": {},
        }

    # ── Lemmas (2-level dotted ids: "1.1", "2.3") ──
    lemma_ids = _extract_all_ids(text, "LEMMA_STATEMENT")
    for lid_str in lemma_ids:
        parts = _parse_dotted_id(lid_str)
        if parts is None or not parts:
            continue
        pid = parts[0]
        lid = parts[1] if len(parts) > 1 else 1
        _ensure_parent_chain(propositions, [pid])
        propositions[pid]["lemmas"][lid] = {
            "statement": _extract_tag(text, "LEMMA_STATEMENT", lid_str) or "",
            "proof": _extract_tag(text, "LEMMA_PROOF", lid_str),
            "claims": {},
        }

    # ── Claims (3-level dotted ids: "1.1.1", "2.3.2") ──
    claim_ids = _extract_all_ids(text, "CLAIM_STATEMENT")
    for cid_str in claim_ids:
        parts = _parse_dotted_id(cid_str)
        if parts is None or not parts:
            continue
        pid, lid = parts[0], parts[1] if len(parts) > 1 else 1
        cid = parts[2] if len(parts) > 2 else 1
        _ensure_parent_chain(propositions, [pid, lid])
        propositions[pid]["lemmas"][lid]["claims"][cid] = {
            "statement": _extract_tag(text, "CLAIM_STATEMENT", cid_str) or "",
            "proof": _extract_tag(text, "CLAIM_PROOF", cid_str),
            "facts": {},
        }

    # ── Facts (4-level dotted ids: "1.1.1.1", ...) ──
    fact_ids = _extract_all_ids(text, "FACT_STATEMENT")
    for fid_str in fact_ids:
        parts = _parse_dotted_id(fid_str)
        if parts is None or not parts:
            continue
        pid, lid = parts[0], parts[1] if len(parts) > 1 else 1
        cid = parts[2] if len(parts) > 2 else 1
        fid = parts[3] if len(parts) > 3 else 1
        _ensure_parent_chain(propositions, [pid, lid, cid])
        propositions[pid]["lemmas"][lid]["claims"][cid]["facts"][fid] = {
            "statement": _extract_tag(text, "FACT_STATEMENT", fid_str) or "",
            "proof": _extract_tag(text, "FACT_PROOF", fid_str),
        }

    # ── Raw sections (all tag contents in document order) ──
    all_tags = "|".join(
        [
            "THEOREM_STATEMENT",
            "PROPOSITION_STATEMENT",
            "LEMMA_STATEMENT",
            "CLAIM_STATEMENT",
            "FACT_STATEMENT",
            "FACT_PROOF",
            "CLAIM_PROOF",
            "LEMMA_PROOF",
            "PROPOSITION_PROOF",
            "THEOREM_PROOF",
        ]
    )
    raw_pattern = rf"<({all_tags})(?:\s[^>]*)?>(.+?)</\1>"
    raw_sections = [
        m.group(2).strip() for m in re.finditer(raw_pattern, text, re.DOTALL)
    ]

    return {
        "preamble": "",
        "theorems": theorems,
        "propositions": propositions,
        "_raw_sections": raw_sections,
    }


def _get_content_for_tag_paper(
    decomposition: Dict[str, Any], tag_name: str, tag_id: Optional[str]
) -> Optional[str]:
    """Look up the content for a given tag from a paper-form decomposition.

    Mirrors ``_get_content_for_tag`` but the theorem level is a dict
    keyed by integer id.
    """
    if tag_name in ("THEOREM_STATEMENT", "THEOREM_PROOF"):
        if tag_id is None:
            return None
        try:
            tid = int(tag_id)
        except ValueError:
            return None
        thm = decomposition["theorems"].get(tid)
        if thm is None:
            return None
        if tag_name == "THEOREM_STATEMENT":
            return thm["statement"]
        return thm["proof"]

    if tag_id is None:
        return None

    parts = _parse_dotted_id(tag_id)
    if parts is None or not parts:
        return None
    props = decomposition["propositions"]
    pid = parts[0]

    if tag_name == "PROPOSITION_STATEMENT":
        return props.get(pid, {}).get("statement")
    if tag_name == "PROPOSITION_PROOF":
        return props.get(pid, {}).get("proof")

    lid = parts[1] if len(parts) > 1 else 1
    lemma = props.get(pid, {}).get("lemmas", {}).get(lid)
    if lemma is None:
        return None
    if tag_name == "LEMMA_STATEMENT":
        return lemma.get("statement")
    if tag_name == "LEMMA_PROOF":
        return lemma.get("proof")

    cid = parts[2] if len(parts) > 2 else 1
    claim = lemma.get("claims", {}).get(cid)
    if claim is None:
        return None
    if tag_name == "CLAIM_STATEMENT":
        return claim.get("statement")
    if tag_name == "CLAIM_PROOF":
        return claim.get("proof")

    fid = parts[3] if len(parts) > 3 else 1
    fact = claim.get("facts", {}).get(fid)
    if fact is None:
        return None
    if tag_name == "FACT_STATEMENT":
        return fact.get("statement")
    if tag_name == "FACT_PROOF":
        return fact.get("proof")

    return None


def _reconstruct_paper_from_decomposition(
    decomposition: Dict[str, Any], original: Optional[str] = None
) -> str:
    """Reconstruct the XML-tagged paper text from a paper-form decomposition.

    If *original* is provided, tags are emitted in the same order they appear
    in the original text. Otherwise falls back to sorted-id order with all
    theorems first, then propositions/lemmas/claims/facts, then theorem proofs.
    """
    if original is not None:
        tag_order = _extract_tag_order(original)
        parts: List[str] = []
        for tag_name, tag_id in tag_order:
            content = _get_content_for_tag_paper(decomposition, tag_name, tag_id)
            if content is None:
                continue
            if tag_id is not None:
                parts.append(f'<{tag_name} id="{tag_id}">\n{content}\n</{tag_name}>')
            else:
                parts.append(f"<{tag_name}>\n{content}\n</{tag_name}>")
        return "\n\n".join(parts)

    # Fallback: sorted order. Theorem statements first (by id), then
    # propositions / lemmas / claims / facts in order, then theorem proofs.
    parts = []
    theorems = decomposition["theorems"]
    for tid in sorted(theorems):
        parts.append(
            f'<THEOREM_STATEMENT id="{tid}">\n{theorems[tid]["statement"]}\n</THEOREM_STATEMENT>'
        )

    for pid in sorted(decomposition["propositions"]):
        prop = decomposition["propositions"][pid]
        parts.append(
            f'<PROPOSITION_STATEMENT id="{pid}">\n{prop["statement"]}\n</PROPOSITION_STATEMENT>'
        )

        for lid in sorted(prop.get("lemmas", {})):
            lemma = prop["lemmas"][lid]
            parts.append(
                f'<LEMMA_STATEMENT id="{pid}.{lid}">\n{lemma["statement"]}\n</LEMMA_STATEMENT>'
            )

            for cid in sorted(lemma.get("claims", {})):
                claim = lemma["claims"][cid]
                parts.append(
                    f'<CLAIM_STATEMENT id="{pid}.{lid}.{cid}">\n{claim["statement"]}\n</CLAIM_STATEMENT>'
                )

                for fid in sorted(claim.get("facts", {})):
                    fact = claim["facts"][fid]
                    parts.append(
                        f'<FACT_STATEMENT id="{pid}.{lid}.{cid}.{fid}">\n{fact["statement"]}\n</FACT_STATEMENT>'
                    )
                    if fact["proof"] is not None:
                        parts.append(
                            f'<FACT_PROOF id="{pid}.{lid}.{cid}.{fid}">\n{fact["proof"]}\n</FACT_PROOF>'
                        )

                if claim["proof"] is not None:
                    parts.append(
                        f'<CLAIM_PROOF id="{pid}.{lid}.{cid}">\n{claim["proof"]}\n</CLAIM_PROOF>'
                    )

            if lemma["proof"] is not None:
                parts.append(
                    f'<LEMMA_PROOF id="{pid}.{lid}">\n{lemma["proof"]}\n</LEMMA_PROOF>'
                )

        if prop["proof"] is not None:
            parts.append(
                f'<PROPOSITION_PROOF id="{pid}">\n{prop["proof"]}\n</PROPOSITION_PROOF>'
            )

    for tid in sorted(theorems):
        thm = theorems[tid]
        if thm["proof"] is not None:
            parts.append(
                f'<THEOREM_PROOF id="{tid}">\n{thm["proof"]}\n</THEOREM_PROOF>'
            )

    return "\n\n".join(parts)


def verify_paper_parse_roundtrip(
    original: str, decomposition: Dict[str, Any], is_identical: bool = True
) -> Dict[str, Any]:
    """Paper-form variant of ``verify_parse_roundtrip`` (multi-theorem).

    Returns ``{"success": True}`` or ``{"success": False, "errors": [...]}``.
    """
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
                        errors.append(
                            f"Missing FACT_STATEMENT id={pid}.{lid}.{cid}.{fid}"
                        )
                    if fact["proof"] is None:
                        errors.append(
                            f"Missing FACT_PROOF id={pid}.{lid}.{cid}.{fid}"
                        )

    if is_identical:
        reconstructed = _reconstruct_paper_from_decomposition(
            decomposition, original=original
        )
        orig_norm = _normalize_for_comparison(original)
        recon_norm = _normalize_for_comparison(reconstructed)
        if orig_norm != recon_norm:
            errors.append(
                "Roundtrip mismatch: reconstructed paper differs from original"
            )

    if errors:
        return {"success": False, "errors": errors}
    return {"success": True}


def verify_parse_roundtrip(
    original: str, decomposition: Dict[str, Any], is_identical=True
) -> Dict[str, Any]:
    """Validate parsing by reconstructing the proof from the decomposition
    and comparing against the original text.

    Returns ``{"success": True}`` when the reconstructed text matches, or
    ``{"success": False, "errors": [...]}`` with a list of problems.
    Also returns the ``reconstructed`` text for inspection.
    """
    errors: List[str] = []

    # Basic completeness checks
    if not decomposition["theorem"]["statement"]:
        errors.append("Missing THEOREM_STATEMENT")
    if decomposition["theorem"]["proof"] is None:
        errors.append("Missing THEOREM_PROOF")
    if not decomposition["propositions"]:
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
                        errors.append(
                            f"Missing FACT_STATEMENT id={pid}.{lid}.{cid}.{fid}"
                        )
                    if fact["proof"] is None:
                        errors.append(f"Missing FACT_PROOF id={pid}.{lid}.{cid}.{fid}")

    # Roundtrip: reconstruct and compare
    if is_identical:
        reconstructed = _reconstruct_from_decomposition(
            decomposition, original=original
        )
        orig_norm = _normalize_for_comparison(original)
        recon_norm = _normalize_for_comparison(reconstructed)

        if orig_norm != recon_norm:
            errors.append(
                "Roundtrip mismatch: reconstructed proof differs from original"
            )

    result: Dict[str, Any] = {}
    if errors:
        result["success"] = False
        result["errors"] = errors
    else:
        result["success"] = True
    return result


def _parse_faithfulness_verdict(output: str) -> Tuple[str, Optional[str]]:
    """Extract verdict and error_description from faithfulness check response.

    Returns (verdict, error_description).
    verdict is "FAITHFUL" or "UNFAITHFUL".
    """
    verdict = None
    error_desc = None

    # Primary: JSON in ```json ... ```
    json_block = re.search(r"```json\s*(\{.*?\})\s*```", output, re.DOTALL)
    if json_block:
        try:
            parsed = json.loads(json_block.group(1))
            v = parsed.get("verdict")
            if v in ("FAITHFUL", "UNFAITHFUL"):
                verdict = v
                error_desc = parsed.get("error_description")
                issues = parsed.get("issues")
                if issues and isinstance(issues, list) and issues:
                    error_desc = "; ".join(str(i) for i in issues)
        except json.JSONDecodeError:
            pass

    # Fallback: regex for verdict key
    if verdict is None:
        m = re.search(r'"verdict"\s*:\s*"(FAITHFUL|UNFAITHFUL)"', output)
        if m:
            verdict = m.group(1)
            err_m = re.search(r'"error_description"\s*:\s*"((?:[^"\\]|\\.)*)"', output)
            error_desc = err_m.group(1) if err_m else None

    # Default
    if verdict is None:
        verdict = "UNFAITHFUL"

    if verdict == "UNFAITHFUL" and not error_desc:
        error_desc = output

    return verdict, error_desc


class PseudoFormalisationVerifier(Verifier):
    """Rewrites a proof, decomposes it into theorem / propositions / lemmas,
    then verifies each component independently."""

    def __init__(
        self,
        n: int = 1,
        n_verifications: int = 1,
        max_tries: int = 10,
        base: int = 10,
        meta_verify: bool = False,
        hard2verify_step_meta_verify: bool = False,
        faithfulness_check: bool = False,
        model="gpt-5.4-mini-2026-03-17",
        effort="medium",
    ):
        key = os.environ.get("OPENAI_API_KEY")
        self.client = AsyncOpenAI(api_key=key)
        self.n = n
        self.n_verifications = n_verifications
        self.max_tries = max_tries
        self.base = base
        self.meta_verify = meta_verify
        self.hard2verify_step_meta_verify = hard2verify_step_meta_verify
        self.faithfulness_check = faithfulness_check
        self.model = model
        self.effort = effort

    async def process_row(self, row) -> dict:
        problem = row["problem"]
        proof = row["proof"]
        original_steps = row.get("original_steps") or []
        if original_steps and not isinstance(original_steps, list):
            original_steps = [str(original_steps)]

        # ── Step 1 & 2: Rewrite + Parse + Faithfulness check ────────────
        max_rewrite_retries = 3
        rewritten_proof = None
        rewrite_usage = None
        decomposition = None
        parse_check = None
        faithfulness_results = None
        accumulated_errors: List[str] = []
        failed_rewrites: List[Dict[str, Any]] = []
        previous_rewrite: Optional[str] = None
        last_iteration_errors: List[str] = []

        for attempt in range(1, max_rewrite_retries + 1):
            # First attempt: rewrite from scratch.
            # Later attempts: regenerate from the previous rewrite + the errors
            # that were flagged on it (instead of starting over from scratch).
            if previous_rewrite is None:
                rewrite_prompt = (
                    f"{REWRITE_PROMPT}\n\n"
                    f"PROBLEM:\n{problem}\n\n"
                    f"PROOF:\n{proof}"
                )
            else:
                errors_text = (
                    "\n".join(f"- {err}" for err in last_iteration_errors)
                    if last_iteration_errors
                    else "- (no specific errors listed)"
                )
                rewrite_prompt = REGENERATE_REWRITE_PROMPT.format(
                    rewrite_instructions=REWRITE_PROMPT,
                    problem=problem,
                    original_proof=proof,
                    previous_rewrite=previous_rewrite,
                    errors=errors_text,
                )

            rewritten_proof, rewrite_usage = await self.async_completion(rewrite_prompt)

            # Parse
            decomposition = parse_rewritten_proof(rewritten_proof)
            parse_check = verify_parse_roundtrip(rewritten_proof, decomposition)
            if not parse_check["success"]:
                continue

            if not self.faithfulness_check:
                break

            # Faithfulness check (component mode)
            faithfulness_results = await self._check_faithfulness(
                problem, proof, decomposition
            )

            # Collect unfaithful components
            unfaithful = [
                (key, res["error_description"])
                for key, res in faithfulness_results.items()
                if res["verdict"] == "UNFAITHFUL"
            ]

            if not unfaithful:
                # All faithful → proceed to verification
                break

            # Save the failed rewrite before moving on
            failed_rewrites.append(
                {
                    "attempt": attempt,
                    "rewritten_proof": rewritten_proof,
                    "decomposition": decomposition,
                    "faithfulness_results": faithfulness_results,
                    "unfaithful_components": {k: d for k, d in unfaithful},
                }
            )

            # Track the errors found on THIS iteration — the regeneration
            # prompt will use only these (paired with the previous rewrite),
            # not the union of errors across all attempts.
            last_iteration_errors = [f"[{key}] {desc}" for key, desc in unfaithful]
            previous_rewrite = rewritten_proof

            # Still maintain accumulated_errors for downstream logging.
            accumulated_errors.extend(last_iteration_errors)

        parse_check["attempts"] = attempt
        parse_check["faithfulness_attempts"] = attempt
        parse_check["faithfulness_results"] = faithfulness_results
        parse_check["accumulated_errors"] = accumulated_errors
        parse_check["failed_rewrites"] = failed_rewrites

        # ── Helper: verify a single component ────────────────────────────
        async def _verify_component(
            label, statement, proof, context_parts, established_parts
        ):
            ctx = "\n\n".join(context_parts) if context_parts else "None"
            est = "\n\n".join(established_parts) if established_parts else "None"
            prompt = COMPONENT_VERIFY_PROMPT.format(
                contexts=ctx,
                assertion=f"{label}: {statement}",
                established_results=est,
                proof=proof,
            )
            output, usage = await self.async_completion(prompt)
            verdict, error_desc, error_class = self._parse_verdict(output)
            return {
                "output": error_desc,
                "usage": usage,
                "score": 7 if verdict == "CORRECT" else 0,
                "error_class": error_class,
            }

        sorted_propositions = sorted(decomposition["propositions"].items())

        # ── Build all verification tasks, then run in parallel ───────────
        verify_tasks: List[Tuple[str, Any]] = []  # (key, coroutine)

        # Lemmas
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                ctx = [
                    f"Theorem: {decomposition['theorem']['statement']}",
                    f"Proposition {p_num}: {prop['statement']}",
                ]
                est = []
                for prev_num, prev_prop in sorted_propositions:
                    if prev_num < p_num:
                        est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
                for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                    if prev_l < l_num:
                        est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                key = f"lemma_{p_num}_{l_num}"
                verify_tasks.append(
                    (
                        key,
                        _verify_component(
                            f"Lemma {p_num}.{l_num}",
                            lemma["statement"],
                            lemma["proof"],
                            ctx,
                            est,
                        ),
                    )
                )

        # Propositions
        for p_num, prop in sorted_propositions:
            ctx = [f"Theorem: {decomposition['theorem']['statement']}"]
            est = []
            for prev_num, prev_prop in sorted_propositions:
                if prev_num < p_num:
                    est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                est.append(f"Lemma {p_num}.{l_num}: {lemma['statement']}")
            key = f"proposition_{p_num}"
            verify_tasks.append(
                (
                    key,
                    _verify_component(
                        f"Proposition {p_num}",
                        prop["statement"],
                        prop["proof"],
                        ctx,
                        est,
                    ),
                )
            )

        # Theorem
        est = [
            f"Proposition {p_num}: {prop['statement']}"
            for p_num, prop in sorted_propositions
        ]
        verify_tasks.append(
            (
                "theorem",
                _verify_component(
                    "Theorem",
                    decomposition["theorem"]["statement"],
                    decomposition["theorem"]["proof"],
                    [],
                    est,
                ),
            )
        )

        # Fire all component verifications in parallel
        keys = [k for k, _ in verify_tasks]
        results = await asyncio.gather(*(coro for _, coro in verify_tasks))
        component_results: Dict[str, Any] = dict(zip(keys, results))

        # ── Aggregate block scores ────────────────────────────────────────
        all_scores: List[int] = [
            r["score"] for r in component_results.values() if r["score"] is not None
        ]

        block_score = min(all_scores) if all_scores else 0

        # ── Step 6 (optional): Meta-verification ────────────────────────
        meta_result = None
        if self.meta_verify:
            # Collect all potential errors (INCORRECT components)
            error_parts: List[str] = [
                f"[{key}]\n{res['output']}"
                for key, res in component_results.items()
                if res["score"] == 0
            ]

            if self.hard2verify_step_meta_verify and original_steps:
                errors_text = (
                    "\n\n---\n\n".join(error_parts)
                    if error_parts
                    else "No potential errors were flagged by rewritten-proof verification."
                )
                meta_prompt = HARD2VERIFY_STEP_META_VERIFY_PROMPT.format(
                    problem=problem,
                    steps=_indexed_solution_steps(original_steps),
                    rewritten_proof=rewritten_proof,
                    errors=errors_text,
                    num_steps=len(original_steps),
                )
                meta_output, meta_usage = await self.async_completion(meta_prompt)
                parsed = _parse_step_meta_verification(
                    meta_output,
                    len(original_steps),
                )
                meta_score = (
                    7
                    if parsed["valid"] and parsed["first_incorrect_step"] == -1
                    else 0
                )
                meta_result = {
                    "mode": "hard2verify_step",
                    "output": meta_output,
                    "usage": meta_usage,
                    "score": meta_score,
                    "parsed": parsed,
                    "num_errors_reviewed": len(error_parts),
                    "num_original_steps": len(original_steps),
                }
                final_score = meta_score
            elif error_parts:
                errors_text = "\n\n---\n\n".join(error_parts)
                meta_prompt = META_VERIFY_PROMPT.format(
                    problem=problem,
                    original_proof=proof,
                    proof=rewritten_proof,
                    errors=errors_text,
                )
                meta_output, meta_usage = await self.async_completion(meta_prompt)
                meta_score = parse_score(meta_output)
                meta_result = {
                    "output": meta_output,
                    "usage": meta_usage,
                    "score": meta_score,
                    "num_errors_reviewed": len(error_parts),
                }
                final_score = meta_score
            else:
                # No errors found by block verifiers → 7
                final_score = 7
        else:
            final_score = block_score

        return {
            "rewritten_proof": rewritten_proof,
            "rewrite_usage": rewrite_usage,
            "decomposition": decomposition,
            "parse_check": parse_check,
            "component_verifications": component_results,
            "meta_verification": meta_result,
            "block_score": block_score,
            "score": final_score,
            "step_verification": meta_result
            if meta_result and meta_result.get("mode") == "hard2verify_step"
            else None,
        }

    @staticmethod
    def _parse_verdict(output: str) -> Tuple[str, Optional[str], Optional[str]]:
        """Extract verdict, error_description, and error_class from the LLM response.

        Returns (verdict, error_description, error_class). error_class is one of
        {"a", "b", "c"} for INCORRECT verdicts, or None otherwise (and also None
        when the model omits the field).
        For INCORRECT verdicts, error_description falls back to the full raw
        output if JSON parsing fails, so we never lose information.
        """
        verdict = None
        error_desc = None
        error_class = None

        def _normalize_class(v):
            if v is None:
                return None
            s = str(v).strip().lower()
            return s if s in ("a", "b", "c") else None

        # Primary: try to parse the JSON block in ```json ... ```
        json_block = re.search(r"```json\s*(\{.*?\})\s*```", output, re.DOTALL)
        if json_block:
            try:
                parsed = json.loads(json_block.group(1))
                v = parsed.get("verdict")
                if v in ("CORRECT", "INCORRECT"):
                    verdict = v
                    error_desc = parsed.get("error_description")
                    error_class = _normalize_class(parsed.get("error_class"))
            except json.JSONDecodeError:
                pass

        # Fallback: find "verdict" key via regex (handles malformed JSON)
        if verdict is None:
            json_match = re.search(r'"verdict"\s*:\s*"(CORRECT|INCORRECT)"', output)
            if json_match:
                verdict = json_match.group(1)
                err_match = re.search(
                    r'"error_description"\s*:\s*"((?:[^"\\]|\\.)*)"', output
                )
                error_desc = err_match.group(1) if err_match else None
                cls_match = re.search(r'"error_class"\s*:\s*"([abc])"', output)
                if cls_match:
                    error_class = cls_match.group(1)

        # Fallback: #CORRECT/#INCORRECT
        if verdict is None:
            if "#CORRECT" in output and "#INCORRECT" not in output:
                verdict = "CORRECT"

        # Default to INCORRECT if nothing matched
        if verdict is None:
            verdict = "INCORRECT"

        # For INCORRECT verdicts, always ensure we have some explanation.
        # Fall back to the full raw output if error_description was not parsed.
        if verdict == "INCORRECT" and not error_desc:
            error_desc = output

        # error_class is only meaningful when INCORRECT
        if verdict != "INCORRECT":
            error_class = None

        return verdict, error_desc, error_class

    async def _check_faithfulness(
        self,
        problem: str,
        original_proof: str,
        decomposition: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Check every component of the rewrite for faithfulness against the original.

        Uses the same context/established-results structure as component
        verification, but with the faithfulness prompt instead.
        Supports 2 levels: propositions, lemmas.
        """

        async def _faith_check(
            label, statement, proof, context_parts, established_parts
        ):
            ctx = "\n\n".join(context_parts) if context_parts else "None"
            est = "\n\n".join(established_parts) if established_parts else "None"
            prompt = COMPONENT_FAITHFULNESS_PROMPT.format(
                problem=problem,
                original_proof=original_proof,
                contexts=ctx,
                established_results=est,
                assertion=f"{label}: {statement}",
                proof=proof or "",
            )
            output, usage = await self.async_completion(prompt)
            verdict, error_desc = _parse_faithfulness_verdict(output)
            return {"verdict": verdict, "error_description": error_desc, "usage": usage}

        sorted_propositions = sorted(decomposition["propositions"].items())
        faith_tasks: List[Tuple[str, Any]] = []  # (key, coroutine)

        # ── Lemmas ──────────────────────────────────────────────────
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                ctx = [
                    f"Theorem: {decomposition['theorem']['statement']}",
                    f"Proposition {p_num}: {prop['statement']}",
                ]
                est = []
                for prev_num, prev_prop in sorted_propositions:
                    if prev_num < p_num:
                        est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
                for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                    if prev_l < l_num:
                        est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                faith_tasks.append(
                    (
                        f"lemma_{p_num}_{l_num}",
                        _faith_check(
                            f"Lemma {p_num}.{l_num}",
                            lemma["statement"],
                            lemma["proof"],
                            ctx,
                            est,
                        ),
                    )
                )

        # ── Propositions ────────────────────────────────────────────
        for p_num, prop in sorted_propositions:
            ctx = [f"Theorem: {decomposition['theorem']['statement']}"]
            est = []
            for prev_num, prev_prop in sorted_propositions:
                if prev_num < p_num:
                    est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                est.append(f"Lemma {p_num}.{l_num}: {lemma['statement']}")
            faith_tasks.append(
                (
                    f"proposition_{p_num}",
                    _faith_check(
                        f"Proposition {p_num}",
                        prop["statement"],
                        prop["proof"],
                        ctx,
                        est,
                    ),
                )
            )

        # ── Theorem ─────────────────────────────────────────────────
        est = [
            f"Proposition {p_num}: {prop['statement']}"
            for p_num, prop in sorted_propositions
        ]
        faith_tasks.append(
            (
                "theorem",
                _faith_check(
                    "Theorem",
                    decomposition["theorem"]["statement"],
                    decomposition["theorem"]["proof"],
                    [],
                    est,
                ),
            )
        )

        # Fire all faithfulness checks in parallel
        keys = [k for k, _ in faith_tasks]
        vals = await asyncio.gather(*(coro for _, coro in faith_tasks))
        return dict(zip(keys, vals))

    async def async_completion(
        self,
        prompt: str,
        system_prompt: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        tokens_used: Dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
            "final_output_tokens": 0,
        }
        if "mini" in self.model:
            max_tokens = 25000
        else:
            max_tokens = 50000

        for attempt in range(self.max_tries):
            try:
                start = time.perf_counter()
                resp = await self.client.responses.create(
                    model=self.model,
                    instructions=system_prompt or None,
                    input=[{"role": "user", "content": prompt}],
                    text={"format": {"type": "text"}},
                    max_output_tokens=max_tokens,
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
                retryable = any(
                    s in msg
                    for s in (
                        "rate",
                        "429",
                        "timeout",
                        "temporarily",
                        "overloaded",
                        " 5",
                    )
                )
                print(
                    f"!! API error (attempt {attempt + 1}/{self.max_tries}): "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                if attempt == self.max_tries - 1 or not retryable:
                    return "", tokens_used
                await asyncio.sleep(self.base * (2**attempt))


class ArxivVerifierBaseline(Verifier):
    """Math-referee baseline for arxiv papers (PDF-only input).

    Sends the rendered PDF of a paper to the model and asks it to
    behave as a peer reviewer: list any mathematical errors and the
    location of each, using the rendered numbering as it appears in
    the PDF (e.g. "Theorem 19", "Lemma 2.3"). One process_row call
    performs one model call and returns a structured result with the
    prompt text, full response, parsed errors, and extracted locations.
    """

    def __init__(
        self,
        model: str = "gpt-5.4-mini-2026-03-17",
        effort: str = "medium",
        max_tries: int = 6,
        base: int = 10,
        max_output_tokens: int = 50000,
    ):
        self.client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.model = model
        self.effort = effort
        self.max_tries = max_tries
        self.base = base
        self.max_output_tokens = max_output_tokens

    async def process_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        prompt_text = ARXIV_REFEREE_PROMPT_PDF_ONLY
        pdf_b64 = row["pdf_b64"]
        pdf_filename = row["pdf_filename"]

        response, usage = await self.async_completion(
            prompt_text=prompt_text,
            pdf_b64=pdf_b64,
            pdf_filename=pdf_filename,
        )
        errors, status = self._parse_errors(response)
        locations = [e.get("location", "") for e in errors]
        return {
            "prompt_text": prompt_text,
            "pdf_filename": pdf_filename,
            "response": response,
            "extracted_errors": errors,
            "extracted_locations": locations,
            "extraction_status": status,
            "usage": usage,
        }

    @staticmethod
    def _parse_errors(
        output: str,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Extract the list of {location, description} error dicts from
        the XML-style format produced by the referee prompt.

        Returns (errors, status). status is one of:
        - "ok"                  parsed at least one well-formed <error>
        - "empty_response"      model returned empty string
        - "no_xml_blocks"       no <error>...</error> blocks present
        - "schema_error"        <error> blocks present but missing fields
        - "no_error_reported"   <errors></errors> block present but empty
        """
        if not output:
            return [], "empty_response"

        error_blocks = re.findall(r"<error>(.*?)</error>", output, re.DOTALL)
        if not error_blocks:
            if re.search(r"<errors>\s*</errors>", output, re.DOTALL):
                return [], "no_error_reported"
            return [], "no_xml_blocks"

        cleaned: List[Dict[str, Any]] = []
        for block in error_blocks:
            loc_m = re.search(r"<location>(.*?)</location>", block, re.DOTALL)
            desc_m = re.search(r"<description>(.*?)</description>", block, re.DOTALL)
            loc = loc_m.group(1).strip() if loc_m else ""
            desc = desc_m.group(1).strip() if desc_m else ""
            if loc or desc:
                cleaned.append({"location": loc, "description": desc})

        if not cleaned:
            return [], "schema_error"
        return cleaned, "ok"

    async def async_completion(
        self,
        prompt_text: str,
        pdf_b64: str,
        pdf_filename: str,
        system_prompt: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        tokens_used: Dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
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
                    max_output_tokens=self.max_output_tokens,
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
                retryable = any(
                    s in msg
                    for s in (
                        "rate",
                        "429",
                        "timeout",
                        "temporarily",
                        "overloaded",
                        " 5",
                    )
                )
                print(
                    f"!! API error (attempt {attempt + 1}/{self.max_tries}): "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                if attempt == self.max_tries - 1 or not retryable:
                    return "", tokens_used
                await asyncio.sleep(self.base * (2**attempt))


class ArxivDecomposedVerifier(Verifier):
    """Decomposed-rewriter verifier for arxiv papers.

    Mirrors ``PseudoFormalisationVerifier`` (IMO version) but adapted for
    whole-paper input: the rewrite contains MULTIPLE top-level theorems
    (one per top-level result the paper proves), shares a single pool of
    propositions across all theorems, and the meta-verification step
    produces a list of errors with locations keyed to the rendered PDF
    labels rather than a 0-7 score.

    Stage summary (one ``process_row`` call), PDF-only input:
      1. Rewrite the paper (from the PDF) into the structured XML form
         ``<THEOREM_STATEMENT id="N">...</THEOREM_STATEMENT>``, then
         propositions, lemmas. Up to ``max_rewrite_retries`` faithfulness
         retries.
      2. Parse + structural completeness check (no roundtrip identity).
      3. Faithfulness check per component against the PDF (optional).
      4. Component verification (text-only, no PDF).
      5. Meta-verification (PDF + rewrite + flagged errors → final
         list of errors with PDF-rendered labels). Always runs.
    """

    def __init__(
        self,
        n: int = 1,
        n_verifications: int = 1,
        max_tries: int = 10,
        base: int = 10,
        faithfulness_check: bool = True,
        model: str = "gpt-5.4-mini-2026-03-17",
        effort: str = "medium",
        max_output_tokens: int = 50000,
        max_rewrite_retries: int = 5,
    ):
        self.client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.n = n
        self.n_verifications = n_verifications
        self.max_tries = max_tries
        self.base = base
        self.faithfulness_check = faithfulness_check
        self.model = model
        self.effort = effort
        self.max_output_tokens = max_output_tokens
        self.max_rewrite_retries = max_rewrite_retries

    async def process_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        pdf_b64: str = row["pdf_b64"]
        pdf_filename: str = row["pdf_filename"]

        # ── Step 1+2+3: Rewrite + Parse + Faithfulness check ────────
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
                rewrite_prompt_text = ARXIV_REWRITE_PROMPT_PDF_ONLY
            else:
                errors_text = (
                    "\n".join(f"- {err}" for err in last_iteration_errors)
                    if last_iteration_errors
                    else "- (no specific errors listed)"
                )
                rewrite_prompt_text = ARXIV_REGENERATE_REWRITE_PROMPT_PDF_ONLY.format(
                    rewrite_instructions=ARXIV_REWRITE_PROMPT_PDF_ONLY.split(
                        "Now rewrite the following"
                    )[0],
                    previous_rewrite=previous_rewrite,
                    errors=errors_text,
                )

            rewritten_paper, rewrite_usage = await self._completion_pdf_text(
                prompt_text=rewrite_prompt_text,
                pdf_b64=pdf_b64,
                pdf_filename=pdf_filename,
            )
            # Record this attempt's rewrite token cost regardless of whether
            # the attempt later parses, fails faithfulness, or succeeds —
            # so total token accounting is recoverable from the JSON.
            rewrite_usages.append({"attempt": attempt, "usage": rewrite_usage})

            try:
                decomposition = parse_rewritten_paper(rewritten_paper)
                # Structural completeness only — whole-paper rewrites
                # rephrase prose, so identity-roundtrip is too strict.
                parse_check = verify_paper_parse_roundtrip(
                    rewritten_paper, decomposition, is_identical=False
                )
            except Exception as exc:
                # Unexpected parser/roundtrip exception — treat as a parse
                # failure and let the retry loop handle it instead of
                # crashing the whole sweep.
                decomposition = {
                    "preamble": "",
                    "theorems": {},
                    "propositions": {},
                    "_raw_sections": [],
                }
                parse_check = {
                    "success": False,
                    "errors": [f"parse exception: {exc!r}"],
                }
            if not parse_check["success"]:
                continue

            if not self.faithfulness_check:
                break

            faithfulness_results = await self._check_faithfulness_paper(
                decomposition, pdf_b64, pdf_filename
            )

            unfaithful = [
                (key, res["error_description"])
                for key, res in faithfulness_results.items()
                if res["verdict"] == "UNFAITHFUL"
            ]

            if not unfaithful:
                break

            failed_rewrites.append(
                {
                    "attempt": attempt,
                    "rewritten_paper": rewritten_paper,
                    "rewrite_usage": rewrite_usage,
                    "decomposition": decomposition,
                    "faithfulness_results": faithfulness_results,
                    "unfaithful_components": {k: d for k, d in unfaithful},
                }
            )

            last_iteration_errors = [f"[{key}] {desc}" for key, desc in unfaithful]
            previous_rewrite = rewritten_paper
            accumulated_errors.extend(last_iteration_errors)

        parse_check["attempts"] = attempt
        parse_check["faithfulness_attempts"] = attempt
        parse_check["faithfulness_results"] = faithfulness_results
        parse_check["accumulated_errors"] = accumulated_errors
        parse_check["failed_rewrites"] = failed_rewrites
        parse_check["rewrite_usages"] = rewrite_usages

        # If parse never succeeded after all retries, return early with a
        # clean failure result. Downstream steps assume a valid decomposition
        # so running them with garbage would crash or produce nonsense.
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
            }

        # ── Step 4: Component verification (text-only) ──────────────
        component_results = await self._verify_components(decomposition)

        # ── Step 5: Meta-verification (PDF + rewrite + errors)
        # Always runs. Produces final XML error list with PDF labels.
        error_parts: List[str] = [
            f"[{key}]\n{res['output']}"
            for key, res in component_results.items()
            if res["score"] == 0
        ]
        errors_text = (
            "\n\n---\n\n".join(error_parts) if error_parts else "(none flagged)"
        )

        meta_prompt_text = ARXIV_META_VERIFY_PROMPT_PDF_ONLY.format(
            rewritten_paper=rewritten_paper,
            errors=errors_text,
        )
        meta_response, meta_usage = await self._completion_pdf_text(
            prompt_text=meta_prompt_text,
            pdf_b64=pdf_b64,
            pdf_filename=pdf_filename,
        )
        extracted_errors, extraction_status = (
            ArxivVerifierBaseline._parse_errors(meta_response)
        )
        extracted_locations = [e.get("location", "") for e in extracted_errors]

        return {
            "rewritten_paper": rewritten_paper,
            "rewrite_usage": rewrite_usage,
            "decomposition": decomposition,
            "parse_check": parse_check,
            "component_verifications": component_results,
            "meta_verification": {
                "response": meta_response,
                "usage": meta_usage,
                "extracted_errors": extracted_errors,
                "extracted_locations": extracted_locations,
                "extraction_status": extraction_status,
                "num_errors_reviewed": len(error_parts),
            },
            "extracted_errors": extracted_errors,
            "extracted_locations": extracted_locations,
            "extraction_status": extraction_status,
        }

    # ── Component verification ──────────────────────────────────────
    async def _verify_components(
        self, decomposition: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Run COMPONENT_VERIFY_PROMPT on every lemma, proposition, and theorem.

        With multiple theorems sharing a single proposition pool: each
        theorem proof gets all propositions available as established
        results, plus all earlier theorems.
        """
        sorted_propositions = sorted(decomposition["propositions"].items())
        sorted_theorems = sorted(decomposition["theorems"].items())

        async def _verify_component(
            label, statement, proof, context_parts, established_parts
        ):
            ctx = "\n\n".join(context_parts) if context_parts else "None"
            est = "\n\n".join(established_parts) if established_parts else "None"
            prompt = COMPONENT_VERIFY_PROMPT.format(
                contexts=ctx,
                assertion=f"{label}: {statement}",
                established_results=est,
                proof=proof,
            )
            output, usage = await self._completion_text_only(prompt)
            verdict, error_desc, error_class = (
                PseudoFormalisationVerifier._parse_verdict(output)
            )
            return {
                "output": error_desc,
                "usage": usage,
                "score": 7 if verdict == "CORRECT" else 0,
                "error_class": error_class,
            }

        verify_tasks: List[Tuple[str, Any]] = []

        # ── Lemmas ──
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                ctx = [
                    f"Theorem {tid}: {thm['statement']}"
                    for tid, thm in sorted_theorems
                ]
                ctx.append(f"Proposition {p_num}: {prop['statement']}")
                est = []
                for prev_num, prev_prop in sorted_propositions:
                    if prev_num < p_num:
                        est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
                for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                    if prev_l < l_num:
                        est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                verify_tasks.append(
                    (
                        f"lemma_{p_num}_{l_num}",
                        _verify_component(
                            f"Lemma {p_num}.{l_num}",
                            lemma["statement"],
                            lemma["proof"],
                            ctx,
                            est,
                        ),
                    )
                )

        # ── Propositions ──
        for p_num, prop in sorted_propositions:
            ctx = [
                f"Theorem {tid}: {thm['statement']}"
                for tid, thm in sorted_theorems
            ]
            est = []
            for prev_num, prev_prop in sorted_propositions:
                if prev_num < p_num:
                    est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                est.append(f"Lemma {p_num}.{l_num}: {lemma['statement']}")
            verify_tasks.append(
                (
                    f"proposition_{p_num}",
                    _verify_component(
                        f"Proposition {p_num}",
                        prop["statement"],
                        prop["proof"],
                        ctx,
                        est,
                    ),
                )
            )

        # ── Theorems ──
        for tid, thm in sorted_theorems:
            est = [
                f"Proposition {p_num}: {prop['statement']}"
                for p_num, prop in sorted_propositions
            ]
            for prev_tid, prev_thm in sorted_theorems:
                if prev_tid < tid:
                    est.append(f"Theorem {prev_tid}: {prev_thm['statement']}")
            verify_tasks.append(
                (
                    f"theorem_{tid}",
                    _verify_component(
                        f"Theorem {tid}",
                        thm["statement"],
                        thm["proof"],
                        [],
                        est,
                    ),
                )
            )

        keys = [k for k, _ in verify_tasks]
        results = await asyncio.gather(*(coro for _, coro in verify_tasks))
        return dict(zip(keys, results))

    # ── Faithfulness check (paper variant) ──────────────────────────
    async def _check_faithfulness_paper(
        self, decomposition: Dict[str, Any], pdf_b64: str, pdf_filename: str
    ) -> Dict[str, Any]:
        """Per-component faithfulness check using paper-form decomposition.

        PDF-only input: the original paper is supplied to the checker as
        the attached PDF rather than inline LaTeX source.
        """

        async def _faith_check(
            label, statement, proof, context_parts, established_parts
        ):
            ctx = "\n\n".join(context_parts) if context_parts else "None"
            est = "\n\n".join(established_parts) if established_parts else "None"
            prompt = ARXIV_COMPONENT_FAITHFULNESS_PROMPT_PDF_ONLY.format(
                contexts=ctx,
                established_results=est,
                assertion=f"{label}: {statement}",
                proof=proof or "",
            )
            output, usage = await self._completion_pdf_text(
                prompt_text=prompt,
                pdf_b64=pdf_b64,
                pdf_filename=pdf_filename,
            )
            verdict, error_desc = _parse_faithfulness_verdict(output)
            return {"verdict": verdict, "error_description": error_desc, "usage": usage}

        sorted_propositions = sorted(decomposition["propositions"].items())
        sorted_theorems = sorted(decomposition["theorems"].items())
        faith_tasks: List[Tuple[str, Any]] = []

        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                ctx = [
                    f"Theorem {tid}: {thm['statement']}"
                    for tid, thm in sorted_theorems
                ]
                ctx.append(f"Proposition {p_num}: {prop['statement']}")
                est = []
                for prev_num, prev_prop in sorted_propositions:
                    if prev_num < p_num:
                        est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
                for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                    if prev_l < l_num:
                        est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                faith_tasks.append(
                    (
                        f"lemma_{p_num}_{l_num}",
                        _faith_check(
                            f"Lemma {p_num}.{l_num}",
                            lemma["statement"],
                            lemma["proof"],
                            ctx,
                            est,
                        ),
                    )
                )

        for p_num, prop in sorted_propositions:
            ctx = [
                f"Theorem {tid}: {thm['statement']}"
                for tid, thm in sorted_theorems
            ]
            est = []
            for prev_num, prev_prop in sorted_propositions:
                if prev_num < p_num:
                    est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                est.append(f"Lemma {p_num}.{l_num}: {lemma['statement']}")
            faith_tasks.append(
                (
                    f"proposition_{p_num}",
                    _faith_check(
                        f"Proposition {p_num}",
                        prop["statement"],
                        prop["proof"],
                        ctx,
                        est,
                    ),
                )
            )

        for tid, thm in sorted_theorems:
            est = [
                f"Proposition {p_num}: {prop['statement']}"
                for p_num, prop in sorted_propositions
            ]
            for prev_tid, prev_thm in sorted_theorems:
                if prev_tid < tid:
                    est.append(f"Theorem {prev_tid}: {prev_thm['statement']}")
            faith_tasks.append(
                (
                    f"theorem_{tid}",
                    _faith_check(
                        f"Theorem {tid}",
                        thm["statement"],
                        thm["proof"],
                        [],
                        est,
                    ),
                )
            )

        keys = [k for k, _ in faith_tasks]
        vals = await asyncio.gather(*(coro for _, coro in faith_tasks))
        return dict(zip(keys, vals))

    # ── Completion helpers ───────────────────────────────────────────
    async def _completion_pdf_text(
        self,
        prompt_text: str,
        pdf_b64: str,
        pdf_filename: str,
        system_prompt: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        """LLM call with PDF file attached + text prompt."""
        tokens_used: Dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
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
                    max_output_tokens=self.max_output_tokens,
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
                retryable = any(
                    s in msg
                    for s in (
                        "rate", "429", "timeout", "temporarily", "overloaded", " 5",
                    )
                )
                print(
                    f"!! API error (attempt {attempt + 1}/{self.max_tries}): "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                if attempt == self.max_tries - 1 or not retryable:
                    return "", tokens_used
                await asyncio.sleep(self.base * (2 ** attempt))

    async def _completion_text_only(
        self,
        prompt: str,
        system_prompt: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        """LLM call with text-only input (component verify, faithfulness)."""
        tokens_used: Dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
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
                    max_output_tokens=self.max_output_tokens,
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
                retryable = any(
                    s in msg
                    for s in (
                        "rate", "429", "timeout", "temporarily", "overloaded", " 5",
                    )
                )
                print(
                    f"!! API error (attempt {attempt + 1}/{self.max_tries}): "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                if attempt == self.max_tries - 1 or not retryable:
                    return "", tokens_used
                await asyncio.sleep(self.base * (2 ** attempt))
