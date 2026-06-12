from src.verifier.complex_prompts import (
    WITHOUT_REFERENCE_PROMPT,
    REWRITE_PROMPT,
    REGENERATE_REWRITE_PROMPT,
    COMPONENT_VERIFY_PROMPT,
    META_VERIFY_PROMPT,
    COMPONENT_FAITHFULNESS_PROMPT,
    GLOBAL_BLOCK_CHECK_PROMPT,
)
import os
import asyncio
import re
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


def _parse_dotted_id(id_str: str) -> List[int]:
    """Parse a dotted id string like '1.2.3' into [1, 2, 3]."""
    return [int(x) for x in id_str.split(".")]


def _parse_deps_tags(text: str) -> Dict[str, List[str]]:
    """Parse every <DEPS id="..."> tag. Returns {tag_id: [cited_id, ...]}.

    The special id "theorem" is used for the DEPS tag that annotates
    THEOREM_PROOF. Empty bodies map to empty lists.
    """
    pattern = r'<DEPS\s+id\s*=\s*"([^"]+)"\s*>(.*?)</DEPS>'
    result: Dict[str, List[str]] = {}
    for m in re.finditer(pattern, text, re.DOTALL):
        tag_id = m.group(1).strip()
        body = m.group(2).strip()
        if not body:
            result[tag_id] = []
        else:
            result[tag_id] = [tok.strip() for tok in body.split(",") if tok.strip()]
    return result


def _lookup_numeric(container: Any, num: int) -> Any:
    """Tolerant child lookup: decomposition dicts use int keys in-memory but
    come back as strings after JSON round-trip, so try both forms."""
    if not isinstance(container, dict):
        return None
    if num in container:
        return container[num]
    return container.get(str(num))


def _format_block_for_established(
    decomposition: Dict[str, Any], id_str: str, include_proof: bool = False
) -> Optional[str]:
    """Resolve a dotted id (e.g. '1.2.3') or the literal 'theorem'/'Theorem' to
    a formatted established-result string like 'Claim 1.2.3: <statement>'.
    When *include_proof* is True, append the block's proof body on a new line
    under a ``Proof:`` label so the entry conveys both statement and proof.
    Returns None if the id is not found in the decomposition.
    """
    def _with_proof(line: str, proof_val: Optional[str]) -> str:
        if include_proof and proof_val:
            return f"{line}\nProof: {proof_val}"
        return line

    if id_str.strip().lower() == "theorem":
        thm = decomposition.get("theorem", {})
        stmt = thm.get("statement")
        if not stmt:
            return None
        return _with_proof(f"Theorem: {stmt}", thm.get("proof"))

    try:
        parts = _parse_dotted_id(id_str)
    except ValueError:
        return None
    props = decomposition.get("propositions", {})
    if not parts:
        return None
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
        propositions[pid]["lemmas"][lid] = {"statement": "", "proof": None, "claims": {}}
    if len(parts) < 3:
        return
    # Ensure claim
    cid = parts[2]
    if cid not in propositions[pid]["lemmas"][lid]["claims"]:
        propositions[pid]["lemmas"][lid]["claims"][cid] = {"statement": "", "proof": None, "facts": {}}


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
            "deps": {id_str: [cited_id, ...]},   # from <DEPS id="..."> tags
            "_raw_sections": list[str],
        }
    """
    theorem_statement = _extract_tag(text, "THEOREM_STATEMENT") or ""
    theorem_proof = _extract_tag(text, "THEOREM_PROOF")

    # ── Propositions (top-level, integer ids) ──
    prop_ids = _extract_all_ids(text, "PROPOSITION_STATEMENT")
    propositions: Dict[int, Dict[str, Any]] = {}
    for pid_str in prop_ids:
        pid = int(pid_str)
        propositions[pid] = {
            "statement": _extract_tag(text, "PROPOSITION_STATEMENT", pid_str) or "",
            "proof": _extract_tag(text, "PROPOSITION_PROOF", pid_str),
            "lemmas": {},
        }

    # ── Lemmas (2-level dotted ids: "1.1", "2.3") ──
    lemma_ids = _extract_all_ids(text, "LEMMA_STATEMENT")
    for lid_str in lemma_ids:
        parts = _parse_dotted_id(lid_str)
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
        pid, lid = parts[0], parts[1] if len(parts) > 1 else 1
        cid = parts[2] if len(parts) > 2 else 1
        fid = parts[3] if len(parts) > 3 else 1
        _ensure_parent_chain(propositions, [pid, lid, cid])
        propositions[pid]["lemmas"][lid]["claims"][cid]["facts"][fid] = {
            "statement": _extract_tag(text, "FACT_STATEMENT", fid_str) or "",
            "proof": _extract_tag(text, "FACT_PROOF", fid_str),
        }

    # ── Raw sections (all tag contents in document order) ──
    all_tags = "|".join([
        "THEOREM_STATEMENT", "PROPOSITION_STATEMENT", "LEMMA_STATEMENT",
        "CLAIM_STATEMENT", "FACT_STATEMENT", "FACT_PROOF", "CLAIM_PROOF",
        "LEMMA_PROOF", "PROPOSITION_PROOF", "THEOREM_PROOF",
    ])
    raw_pattern = rf"<({all_tags})(?:\s[^>]*)?>(.+?)</\1>"
    raw_sections = [m.group(2).strip() for m in re.finditer(raw_pattern, text, re.DOTALL)]

    return {
        "preamble": "",
        "theorem": {
            "statement": theorem_statement,
            "proof": theorem_proof,
        },
        "propositions": propositions,
        "deps": _parse_deps_tags(text),
        "_raw_sections": raw_sections,
    }


def _extract_tag_order(text: str) -> List[Tuple[str, Optional[str]]]:
    """Extract the order of XML tags from the original text.

    Returns a list of (tag_name, id_or_None) in the order they appear.
    """
    all_tags = "|".join([
        "THEOREM_STATEMENT", "PROPOSITION_STATEMENT", "LEMMA_STATEMENT",
        "CLAIM_STATEMENT", "FACT_STATEMENT", "FACT_PROOF", "CLAIM_PROOF",
        "LEMMA_PROOF", "PROPOSITION_PROOF", "THEOREM_PROOF",
    ])
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
        parts.append(f'<PROPOSITION_STATEMENT id="{pid}">\n{prop["statement"]}\n</PROPOSITION_STATEMENT>')

        for lid in sorted(prop.get("lemmas", {})):
            lemma = prop["lemmas"][lid]
            parts.append(f'<LEMMA_STATEMENT id="{pid}.{lid}">\n{lemma["statement"]}\n</LEMMA_STATEMENT>')

            for cid in sorted(lemma.get("claims", {})):
                claim = lemma["claims"][cid]
                parts.append(f'<CLAIM_STATEMENT id="{pid}.{lid}.{cid}">\n{claim["statement"]}\n</CLAIM_STATEMENT>')

                for fid in sorted(claim.get("facts", {})):
                    fact = claim["facts"][fid]
                    parts.append(f'<FACT_STATEMENT id="{pid}.{lid}.{cid}.{fid}">\n{fact["statement"]}\n</FACT_STATEMENT>')
                    if fact["proof"] is not None:
                        parts.append(f'<FACT_PROOF id="{pid}.{lid}.{cid}.{fid}">\n{fact["proof"]}\n</FACT_PROOF>')

                if claim["proof"] is not None:
                    parts.append(f'<CLAIM_PROOF id="{pid}.{lid}.{cid}">\n{claim["proof"]}\n</CLAIM_PROOF>')

            if lemma["proof"] is not None:
                parts.append(f'<LEMMA_PROOF id="{pid}.{lid}">\n{lemma["proof"]}\n</LEMMA_PROOF>')

        if prop["proof"] is not None:
            parts.append(f'<PROPOSITION_PROOF id="{pid}">\n{prop["proof"]}\n</PROPOSITION_PROOF>')

    if thm["proof"] is not None:
        parts.append(f"<THEOREM_PROOF>\n{thm['proof']}\n</THEOREM_PROOF>")

    return "\n\n".join(parts)


def _normalize_for_comparison(text: str) -> str:
    """Normalize whitespace for comparison: collapse runs of whitespace to single space, strip."""
    return re.sub(r"\s+", " ", text).strip()


def _check_deps_ordering(decomposition: Dict[str, Any]) -> List[str]:
    """Validate every DEPS citation is structurally legal. Returns a list of
    error strings (empty when all DEPS are well-formed).

    Rules:
      - The "theorem" block may cite any existing dotted-id block.
      - A dotted-id block C may cite dotted-id D iff D != C,
        D is not an ancestor of C (ancestors are context, not Established
        Results), AND either D is a proper descendant of C OR D < C
        lexicographically (earlier subtree).
      - A dotted-id block may not cite "theorem".
      - Every cited id must resolve to an existing block in the decomposition.

    The graph remains acyclic: post-order traversal is a valid topological
    order (descendants precede their parent; earlier subtrees precede later
    ones), so no cycle is possible.
    """
    deps_map: Dict[str, List[str]] = decomposition.get("deps", {})
    errors: List[str] = []

    def _exists(id_str: str) -> bool:
        return _format_block_for_established(decomposition, id_str) is not None

    def _is_strict_prefix(prefix: Tuple[int, ...], full: Tuple[int, ...]) -> bool:
        return len(prefix) < len(full) and full[: len(prefix)] == prefix

    for citing_id, cited_list in deps_map.items():
        citing_is_theorem = citing_id.strip().lower() == "theorem"

        if citing_is_theorem:
            for cited_id in cited_list:
                if not _exists(cited_id):
                    errors.append(
                        f"DEPS id='theorem': cited id '{cited_id}' does not correspond to any block"
                    )
            continue

        try:
            citing_tuple = tuple(_parse_dotted_id(citing_id))
        except ValueError:
            errors.append(
                f"DEPS id='{citing_id}': citing block id is not a valid dotted id"
            )
            continue

        for cited_id in cited_list:
            if not _exists(cited_id):
                errors.append(
                    f"DEPS id='{citing_id}': cited id '{cited_id}' does not correspond to any block"
                )
                continue
            if cited_id.strip().lower() == "theorem":
                errors.append(
                    f"DEPS id='{citing_id}': cannot cite 'theorem' (theorem is not before a sub-block)"
                )
                continue
            try:
                cited_tuple = tuple(_parse_dotted_id(cited_id))
            except ValueError:
                errors.append(
                    f"DEPS id='{citing_id}': cited id '{cited_id}' is not a valid dotted id"
                )
                continue
            if cited_tuple == citing_tuple:
                errors.append(
                    f"DEPS id='{citing_id}': block cannot cite itself"
                )
                continue
            if _is_strict_prefix(cited_tuple, citing_tuple):
                errors.append(
                    f"DEPS id='{citing_id}': cited id '{cited_id}' is an ancestor of the citing block and cannot be an Established Result"
                )
                continue
            if cited_tuple > citing_tuple and not _is_strict_prefix(citing_tuple, cited_tuple):
                errors.append(
                    f"DEPS id='{citing_id}': cited id '{cited_id}' is not before the citing block and is not a descendant of it"
                )

    return errors


def verify_parse_roundtrip(
    original: str, decomposition: Dict[str, Any],
    is_identical=True
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
                        errors.append(f"Missing FACT_STATEMENT id={pid}.{lid}.{cid}.{fid}")
                    if fact["proof"] is None:
                        errors.append(f"Missing FACT_PROOF id={pid}.{lid}.{cid}.{fid}")

    # DEPS ordering: every cited id must be strictly before the citing block.
    errors.extend(_check_deps_ordering(decomposition))

    # Roundtrip: reconstruct and compare
    if is_identical :
        reconstructed = _reconstruct_from_decomposition(decomposition, original=original)
        # Strip DEPS tags from the original before comparison — DEPS is
        # metadata and is not emitted by _reconstruct_from_decomposition.
        original_no_deps = re.sub(
            r'<DEPS\s+id\s*=\s*"[^"]*"\s*>.*?</DEPS>', "", original, flags=re.DOTALL
        )
        orig_norm = _normalize_for_comparison(original_no_deps)
        recon_norm = _normalize_for_comparison(reconstructed)

        if orig_norm != recon_norm:
            errors.append("Roundtrip mismatch: reconstructed proof differs from original")

    result: Dict[str, Any] = {}
    if errors:
        result["success"] = False
        result["errors"] = errors
    else:
        result["success"] = True
    return result


def _parse_faithfulness_verdict(output: str) -> Tuple[str, Optional[str]]:
    """Extract verdict and error_description from faithfulness check response.

    Expects the tag-based output format produced by COMPONENT_FAITHFULNESS_PROMPT
    (<verdict>, <error_description>). Returns (verdict, error_description).
    verdict is "FAITHFUL" or "UNFAITHFUL".
    """
    def _tag(name: str, text: str) -> Optional[str]:
        m = re.search(rf'<{name}>(.*?)</{name}>', text, re.DOTALL)
        if not m:
            return None
        val = m.group(1).strip()
        return val if val else None

    verdict = None
    error_desc = None

    v_raw = _tag("verdict", output)
    if v_raw:
        v = v_raw.upper()
        if v in ("FAITHFUL", "UNFAITHFUL"):
            verdict = v
        else:
            print(f"[_parse_faithfulness_verdict] <verdict> tag had unexpected value: {v_raw!r}")

    if verdict is not None:
        error_desc = _tag("error_description", output)

    # Fallback: bare-word verdict anywhere in output
    if verdict is None:
        m = re.search(r'\b(FAITHFUL|UNFAITHFUL)\b', output)
        if m:
            verdict = m.group(1)
            print(f"[_parse_faithfulness_verdict] recovered verdict via bare-word fallback: {verdict}")

    # Default
    if verdict is None:
        verdict = "UNFAITHFUL"
        print(f"[_parse_faithfulness_verdict] no verdict found; defaulting to UNFAITHFUL. raw output (first 500 chars):\n{output[:500]}")

    if verdict == "UNFAITHFUL" and not error_desc:
        error_desc = output

    return verdict, error_desc


_MISSING_TAG_RE = re.compile(r"<(lemma|definition)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)


def _extract_missing_tags(text: Optional[str], run_idx: int) -> Dict[str, Dict[str, str]]:
    """Parse <lemma>/<definition> tags out of a block-verifier run's output.

    Returns a dict keyed by tag id (`missing_lemma_{run_idx}.{k}` or
    `missing_definition_{run_idx}.{k}`) → {"type", "content"}. Lemma and
    definition use independent 1-based counters within the run, per the
    user's "Missing_Lemma 1.3 = 3rd missing lemma of run 1" convention.
    """
    if not text:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    lemma_i = 0
    def_i = 0
    for m in _MISSING_TAG_RE.finditer(text):
        kind = m.group(1).lower()
        content = m.group(2).strip()
        if not content:
            continue
        if kind == "lemma":
            lemma_i += 1
            tag_id = f"missing_lemma_{run_idx}.{lemma_i}"
        else:
            def_i += 1
            tag_id = f"missing_definition_{run_idx}.{def_i}"
        out[tag_id] = {"type": kind, "content": content}
    return out


def _parse_global_check_json(output: str) -> Dict[str, Any]:
    """Extract the JSON dict from a global-block-check response."""
    m = re.search(r"```json\s*(\{.*\})\s*```", output, re.DOTALL)
    if not m:
        m = re.search(r"(\{.*\})", output, re.DOTALL)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _block_key_to_label(key: str) -> str:
    """Map an internal component_results key (e.g. 'fact_1_2_3_4') to the
    display label used in the rewritten proof (e.g. 'Fact 1.2.3.4')."""
    if key == "theorem":
        return "Theorem"
    kind, *nums = key.split("_")
    return f"{kind.capitalize()} {'.'.join(nums)}"


def _block_key_to_dotted_id(key: str) -> Optional[str]:
    """Inverse direction used when comparing against ids returned by
    GLOBAL_BLOCK_CHECK_PROMPT: 'proposition_1' → '1', 'lemma_1_2' → '1.2',
    'claim_1_2_3' → '1.2.3', 'fact_1_2_3_4' → '1.2.3.4', 'theorem' → 'theorem'."""
    if key == "theorem":
        return "theorem"
    parts = key.split("_")
    if len(parts) < 2:
        return None
    return ".".join(parts[1:])


class ComplexPseudoFormalisationVerifier(Verifier):
    """Rewrites a proof, decomposes it into theorem / propositions / lemmas,
    then verifies each component independently."""

    def __init__(
        self,
        n: int = 1,
        n_verifications: int = 1,
        max_tries: int = 10,
        base: int = 10,
        meta_verify: bool = False,
        faithfulness_check: bool = False,
        block_verifier: bool = True,
        global_block_check: bool = False,
        m_re: int = 5,
        model='gpt-5.4-mini-2026-03-17',
        effort="medium",
        max_tokens=25000,
        verbose: bool = False,
        max_rewrite_retries : int=3,
    ):
        key = os.environ.get("OPENAI_API_KEY")
        self.client = AsyncOpenAI(api_key=key)
        self.n = n
        self.n_verifications = n_verifications
        self.max_tries = max_tries
        self.base = base
        self.meta_verify = meta_verify
        self.faithfulness_check = faithfulness_check
        self.block_verifier = block_verifier
        self.global_block_check = global_block_check
        self.m_re = m_re
        self.model=model
        self.effort=effort
        self.max_tokens=max_tokens
        self.verbose = verbose
        self.max_rewrite_retries=max_rewrite_retries

    async def process_row(self, row) -> dict:
        problem = row["problem"]
        proof = row["proof"]

        # ── Step 1 & 2: Rewrite + Parse + Faithfulness check ────────────
        max_rewrite_retries = self.max_rewrite_retries
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
            # print(rewrite_prompt)
            if self.verbose:
                print(f"[verbose] Generating rewritten proof (attempt {attempt})...")
            rewritten_proof, rewrite_usage = await self.async_completion(rewrite_prompt)
            if self.verbose:
                print(f"[verbose] Rewriting proof complete (attempt {attempt})")

            # Parse
            decomposition = parse_rewritten_proof(rewritten_proof)
            parse_check = verify_parse_roundtrip(rewritten_proof, decomposition)
            if not parse_check["success"]:
                continue

            if not self.faithfulness_check:
                break

            # Faithfulness check (component mode)
            if self.verbose:
                print(f"[verbose] Starting faithfulness check (attempt {attempt})...")
            faithfulness_results = await self._check_faithfulness(
                problem, proof, decomposition
            )
            if self.verbose:
                print(f"[verbose] Faithfulness check complete (attempt {attempt})")

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
            failed_rewrites.append({
                "attempt": attempt,
                "rewritten_proof": rewritten_proof,
                "decomposition": decomposition,
                "faithfulness_results": faithfulness_results,
                "unfaithful_components": {k: d for k, d in unfaithful},
            })

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
        async def _verify_component(label, statement, proof, context_parts, established_parts):
            ctx = "\n\n".join(context_parts) if context_parts else "None"
            est = "\n\n".join(established_parts) if established_parts else "None"
            prompt = COMPONENT_VERIFY_PROMPT.format(
                contexts=ctx,
                assertion=f"{label}: {statement}",
                established_results=est,
                proof=proof,
            )
            output, usage = await self.async_completion(prompt,is_websearch=True)
            verdict, combined = self._parse_verdict(output)
            # return {"output": combined, "usage": usage, "score": 7 if verdict == "CORRECT" else 0}
            return {"output": combined,"llm_output" : output ,"usage": usage, "score": 7 if verdict == "CORRECT" else 0}

        async def _verify_component_multi(label, statement, proof, context_parts, established_parts):
            """Run self.n_verifications independent verifications per block in parallel
            and aggregate via majority vote. Returns {"output", "usage", "score", "runs"}."""
            m = max(1, int(getattr(self, "n_verifications", 1)))
            if m == 1:
                r = await _verify_component(label, statement, proof, context_parts, established_parts)
                return {**r, "runs": [r]}
            runs = await asyncio.gather(*[
                _verify_component(label, statement, proof, context_parts, established_parts)
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

        sorted_propositions = sorted(decomposition["propositions"].items())
            # return {"output": error_desc, "usage": usage, "score": 7 if verdict == "CORRECT" else 0}

        deps_map: Dict[str, List[str]] = decomposition.get("deps", {})

        def _augment_est_with_deps(est: List[str], proof_id: str) -> None:
            """Append statements of DEPS-cited blocks to *est* (in-place, deduped)."""
            seen = set(est)
            for cited_id in deps_map.get(proof_id, []):
                formatted = _format_block_for_established(decomposition, cited_id)
                if formatted is not None and formatted not in seen:
                    est.append(formatted)
                    seen.add(formatted)

        # ── Build all verification tasks, then run in parallel ───────────
        verify_tasks: List[Tuple[str, Any]] = []  # (key, coroutine)

        # Facts
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    for f_num, fact in sorted(claim.get("facts", {}).items()):
                        ctx = [
                            f"Theorem: {decomposition['theorem']['statement']}",
                            f"Proposition {p_num}: {prop['statement']}",
                            f"Lemma {p_num}.{l_num}: {lemma['statement']}",
                            f"Claim {p_num}.{l_num}.{c_num}: {claim['statement']}",
                        ]
                        est = []
                        for prev_num, prev_prop in sorted_propositions:
                            if prev_num < p_num:
                                est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
                        for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                            if prev_l < l_num:
                                est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                        for prev_c, prev_claim in sorted(lemma.get("claims", {}).items()):
                            if prev_c < c_num:
                                est.append(f"Claim {p_num}.{l_num}.{prev_c}: {prev_claim['statement']}")
                        for prev_f, prev_fact in sorted(claim.get("facts", {}).items()):
                            if prev_f < f_num:
                                est.append(f"Fact {p_num}.{l_num}.{c_num}.{prev_f}: {prev_fact['statement']}")
                        _augment_est_with_deps(est, f"{p_num}.{l_num}.{c_num}.{f_num}")
                        key = f"fact_{p_num}_{l_num}_{c_num}_{f_num}"
                        verify_tasks.append((key, _verify_component_multi(
                            f"Fact {p_num}.{l_num}.{c_num}.{f_num}", fact["statement"], fact["proof"], ctx, est
                        )))

        # Claims
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    ctx = [
                        f"Theorem: {decomposition['theorem']['statement']}",
                        f"Proposition {p_num}: {prop['statement']}",
                        f"Lemma {p_num}.{l_num}: {lemma['statement']}",
                    ]
                    est = []
                    for prev_num, prev_prop in sorted_propositions:
                        if prev_num < p_num:
                            est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
                    for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                        if prev_l < l_num:
                            est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                    for prev_c, prev_claim in sorted(lemma.get("claims", {}).items()):
                        if prev_c < c_num:
                            est.append(f"Claim {p_num}.{l_num}.{prev_c}: {prev_claim['statement']}")
                    for f_num, fact in sorted(claim.get("facts", {}).items()):
                        est.append(f"Fact {p_num}.{l_num}.{c_num}.{f_num}: {fact['statement']}")
                    _augment_est_with_deps(est, f"{p_num}.{l_num}.{c_num}")
                    key = f"claim_{p_num}_{l_num}_{c_num}"
                    verify_tasks.append((key, _verify_component_multi(
                        f"Claim {p_num}.{l_num}.{c_num}", claim["statement"], claim["proof"], ctx, est
                    )))

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
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    est.append(f"Claim {p_num}.{l_num}.{c_num}: {claim['statement']}")
                _augment_est_with_deps(est, f"{p_num}.{l_num}")
                key = f"lemma_{p_num}_{l_num}"
                verify_tasks.append((key, _verify_component_multi(
                    f"Lemma {p_num}.{l_num}", lemma["statement"], lemma["proof"], ctx, est
                )))

        # Propositions
        for p_num, prop in sorted_propositions:
            ctx = [f"Theorem: {decomposition['theorem']['statement']}"]
            est = []
            for prev_num, prev_prop in sorted_propositions:
                if prev_num < p_num:
                    est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                est.append(f"Lemma {p_num}.{l_num}: {lemma['statement']}")
            _augment_est_with_deps(est, f"{p_num}")
            key = f"proposition_{p_num}"
            verify_tasks.append((key, _verify_component_multi(
                f"Proposition {p_num}", prop["statement"], prop["proof"], ctx, est
            )))

        # Theorem
        est = [f"Proposition {p_num}: {prop['statement']}" for p_num, prop in sorted_propositions]
        _augment_est_with_deps(est, "theorem")
        verify_tasks.append(("theorem", _verify_component_multi(
            "Theorem", decomposition["theorem"]["statement"],
            decomposition["theorem"]["proof"], [], est
        )))

        # Fire all component verifications in parallel
        if self.block_verifier:
            if self.verbose:
                print(f"[verbose] Starting block verification ({len(verify_tasks)} components)...")
            keys = [k for k, _ in verify_tasks]
            results = await asyncio.gather(*(coro for _, coro in verify_tasks))
            component_results: Dict[str, Any] = dict(zip(keys, results))
        else:
            for _, coro in verify_tasks:
                coro.close()
            component_results = {
                k: {"output": None, "usage": None, "score": 7}
                for k, _ in verify_tasks
            }
        if self.verbose:
            print("[verbose] Block verification complete")

        # ── Optional: global block check (diagnostic) ─────────────────────
        # Check whether missing <lemma>/<definition> tags flagged by BV runs
        # are actually addressed in earlier blocks of the rewritten proof.
        # In process_row every run is "new" → examine all 1..len(runs).
        if self.block_verifier and self.global_block_check:
            if self.verbose:
                print("[verbose] Starting global block check...")
            run_indices = {
                k: list(range(1, len(entry.get("runs") or []) + 1))
                for k, entry in component_results.items()
                if entry.get("score") != 7
            }
            await self._apply_global_block_check(
                component_results, rewritten_proof, run_indices,
            )
            await self._apply_global_check_reverification(
                component_results, decomposition,
            )
            if self.verbose:
                print("[verbose] Global block check complete")

        # ── Aggregate block scores ────────────────────────────────────────
        all_scores: List[int] = [
            r["score"] for r in component_results.values() if r["score"] is not None
        ]

        block_score = min(all_scores) if all_scores else 0

        # ── Step 6 (optional): Calibration ────────────────────────
        meta_result = None
        if self.meta_verify:
            # Collect all potential errors (INCORRECT components)
            error_parts: List[str] = [
                f"[{key}]\n{res['output']}"
                for key, res in component_results.items()
                if res["score"] == 0
            ]

            if error_parts:
                errors_text = "\n\n---\n\n".join(error_parts)
                meta_prompt = META_VERIFY_PROMPT.format(
                    problem=problem,
                    original_proof=proof,
                    proof=rewritten_proof,
                    errors=errors_text,
                )
                if self.verbose:
                    print(f"[verbose] Starting meta-verification ({len(error_parts)} errors)...")
                meta_output, meta_usage = await self.async_completion(
                    meta_prompt
                )
                if self.verbose:
                    print("[verbose] Meta-verification complete")
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
        }

    def _build_block_inputs(self, decomposition: Dict[str, Any]) -> List[Tuple[str, str, str, Optional[str], List[str], List[str]]]:
        """Enumerate every verifiable block from a decomposition and build the
        (key, label, statement, proof, ctx_parts, est_parts) tuple for each.
        Uses the same context/established-results rules as process_row and
        augments `est_parts` with DEPS-cited statements."""
        theorem = decomposition.get("theorem", {})
        sorted_propositions = sorted(decomposition.get("propositions", {}).items())
        deps_map: Dict[str, List[str]] = decomposition.get("deps", {})

        def _augment(est: List[str], proof_id: str) -> None:
            seen = set(est)
            for cited in deps_map.get(proof_id, []):
                formatted = _format_block_for_established(decomposition, cited)
                if formatted is not None and formatted not in seen:
                    est.append(formatted)
                    seen.add(formatted)

        blocks: List[Tuple[str, str, str, Optional[str], List[str], List[str]]] = []

        # Facts
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    for f_num, fact in sorted(claim.get("facts", {}).items()):
                        ctx = [
                            f"Theorem: {theorem.get('statement')}",
                            f"Proposition {p_num}: {prop['statement']}",
                            f"Lemma {p_num}.{l_num}: {lemma['statement']}",
                            f"Claim {p_num}.{l_num}.{c_num}: {claim['statement']}",
                        ]
                        est: List[str] = []
                        for prev_num, prev_prop in sorted_propositions:
                            if prev_num < p_num:
                                est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
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
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    ctx = [
                        f"Theorem: {theorem.get('statement')}",
                        f"Proposition {p_num}: {prop['statement']}",
                        f"Lemma {p_num}.{l_num}: {lemma['statement']}",
                    ]
                    est = []
                    for prev_num, prev_prop in sorted_propositions:
                        if prev_num < p_num:
                            est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
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
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                ctx = [
                    f"Theorem: {theorem.get('statement')}",
                    f"Proposition {p_num}: {prop['statement']}",
                ]
                est = []
                for prev_num, prev_prop in sorted_propositions:
                    if prev_num < p_num:
                        est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
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
        for p_num, prop in sorted_propositions:
            ctx = [f"Theorem: {theorem.get('statement')}"]
            est = []
            for prev_num, prev_prop in sorted_propositions:
                if prev_num < p_num:
                    est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                est.append(f"Lemma {p_num}.{l_num}: {lemma['statement']}")
            _augment(est, f"{p_num}")
            blocks.append((
                f"proposition_{p_num}",
                f"Proposition {p_num}",
                prop["statement"], prop["proof"], ctx, est,
            ))

        # Theorem
        est = [f"Proposition {p_num}: {prop['statement']}" for p_num, prop in sorted_propositions]
        _augment(est, "theorem")
        blocks.append((
            "theorem",
            "Theorem",
            theorem.get("statement"), theorem.get("proof"), [], est,
        ))

        return blocks

    async def _run_single_verification(self, label: str, statement: str, proof: Optional[str],
                                       context_parts: List[str], established_parts: List[str]) -> Dict[str, Any]:
        """Run one verification call for a block. Mirrors the local _verify_component
        inside process_row; exposed on the class so topup_verifications can reuse it."""
        ctx = "\n\n".join(context_parts) if context_parts else "None"
        est = "\n\n".join(established_parts) if established_parts else "None"
        prompt = COMPONENT_VERIFY_PROMPT.format(
            contexts=ctx,
            assertion=f"{label}: {statement}",
            established_results=est,
            proof=proof,
        )
        output, usage = await self.async_completion(prompt, is_websearch=True)
        verdict, combined = self._parse_verdict(output)
        return {"output": combined,'llm_output' : output, "usage": usage, "score": 7 if verdict == "CORRECT" else 0}

    async def _run_global_block_check(
        self,
        block_label: str,
        full_proof: str,
        tags_by_id: Dict[str, Dict[str, str]],
    ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Any]]:
        """One LLM call asking whether each flagged missing lemma/definition
        is already addressed in blocks declared earlier than *block_label*.
        Returns (per_tag_results, usage)."""
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
        output, usage = await self.async_completion(prompt)
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
        """Run the global-block-check pass, mutating *component_results* in place.

        For each block in *run_indices_per_block*, examine only the runs at the
        specified 1-based GLOBAL indices into ``component_results[key]["runs"]``
        (so top-up callers can restrict the pass to newly appended runs).
        Skip the block if none of those runs produced <lemma>/<definition>
        tags; otherwise fire one LLM call per block, attribute per-tag answers
        back to each examined run as a ``global_check`` dict (``{}`` if the run
        had no tags of its own), and record the call's usage on the block
        entry as ``global_check_usage``. Does not change any scores.
        """
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
        self,
        component_results: Dict[str, Any],
        decomposition: Dict[str, Any],
    ) -> None:
        """For every block whose runs produced a non-empty ``global_check['<tag>']
        ['Addressed by']`` list, re-run the block verifier ``self.m_re`` times with
        the cited blocks appended to the Established Results list, then replace the
        block's aggregated verdict with a fresh majority vote over the m_re new runs.

        Each cited block contributes one Established Results entry: statement-only
        when ``parts == "statement"``, statement + ``Proof:`` body when
        ``parts == "both"``. Self-references are dropped. Original ``runs`` and the
        pre-reverify aggregate are preserved; new data lands under
        ``reverification_runs`` / ``reverification_score`` /
        ``reverification_added_established_results``. The block's ``score``
        (and ``output``/``usage`` for downstream display) is updated so the
        outer ``block_score = min(...)`` picks up the rescued verdict.
        """
        if not decomposition or not component_results:
            return

        m_re = max(1, int(getattr(self, "m_re", 3)))
        blocks_by_key = {b[0]: b for b in self._build_block_inputs(decomposition)}

        reverify_tasks: List[Tuple[str, Any]] = []
        added_est_by_key: Dict[str, List[str]] = {}

        for key, entry in component_results.items():
            if not isinstance(entry, dict):
                continue
            runs = entry.get("runs") or []

            # Collect {id: parts} across all runs; upgrade 'statement' → 'both'
            # if the same id appears under both modes.
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

            # Enforce the same citation rules as _check_deps_ordering: no
            # self, no ancestor, no later-subtree non-descendant, and no
            # 'theorem' as a cited id for non-theorem blocks.
            self_dotted = _block_key_to_dotted_id(key)
            if self_dotted and self_dotted.lower() != "theorem":
                try:
                    self_tuple = tuple(_parse_dotted_id(self_dotted))
                except ValueError:
                    self_tuple = None
                if self_tuple is not None:
                    def _strict_prefix(prefix, full):
                        return len(prefix) < len(full) and full[: len(prefix)] == prefix
                    for bid in list(by_id):
                        bid_s = bid.strip()
                        if bid_s.lower() == "theorem":
                            del by_id[bid]; continue
                        try:
                            cited_tuple = tuple(_parse_dotted_id(bid_s))
                        except ValueError:
                            del by_id[bid]; continue
                        if cited_tuple == self_tuple:
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
                formatted = _format_block_for_established(
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

    async def topup_verifications(self, entry: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, int]]:
        """Top up each block's verification runs to self.n_verifications by appending
        new calls to any existing ones. Re-aggregates block scores via majority vote
        and updates the entry's block_score/score in place.

        Returns (updated_entry, stats) where stats = {"added": int, "blocks_touched": int,
        "min_existing": int, "max_existing": int}."""
        stats = {"added": 0, "blocks_touched": 0, "min_existing": 0, "max_existing": 0}
        decomposition = entry.get("decomposition")
        if not decomposition:
            return entry, stats

        m = max(1, int(getattr(self, "n_verifications", 1)))
        component_results: Dict[str, Any] = dict(entry.get("component_verifications") or {})

        blocks = self._build_block_inputs(decomposition)

        # For each block, figure out how many runs are already stored.
        # Back-compat: entries from before the `runs` field was added look like
        # {"output", "usage", "score"} — treat them as a single existing run.
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
                existing_runs_per_block[key] = [{"output": cur.get("output"), "usage": cur.get("usage"), "score": cur.get("score")}]
            else:
                existing_runs_per_block[key] = []
            existing_counts.append(len(existing_runs_per_block[key]))

        if existing_counts:
            stats["min_existing"] = min(existing_counts)
            stats["max_existing"] = max(existing_counts)

        # Fire (m - len) additional calls per block in parallel.
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

        # Merge, re-aggregate, and write back.
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

        # Global block check on ONLY the newly appended runs, if enabled.
        if self.global_block_check:
            new_run_indices: Dict[str, List[int]] = {}
            for key, new_runs in new_by_key.items():
                if component_results.get(key, {}).get("score") == 7:
                    continue
                existing_len = len(existing_runs_per_block.get(key, []))
                new_run_indices[key] = list(
                    range(existing_len + 1, existing_len + len(new_runs) + 1)
                )
            if new_run_indices:
                await self._apply_global_block_check(
                    component_results,
                    entry.get("rewritten_proof"),
                    new_run_indices,
                )
                await self._apply_global_check_reverification(
                    component_results, decomposition,
                )

        entry["component_verifications"] = component_results

        # Recompute block_score. If meta_verification isn't stored, bubble the new
        # block_score into "score"; otherwise leave "score" alone so the original
        # meta-verification verdict (if any) isn't silently overwritten.
        all_scores = [r["score"] for r in component_results.values() if r.get("score") is not None]
        block_score = min(all_scores) if all_scores else 0
        entry["block_score"] = block_score
        if not entry.get("meta_verification"):
            entry["score"] = block_score
        return entry, stats

    @staticmethod
    def _parse_verdict(output: str) -> Tuple[str, Optional[str]]:
        """Extract the verdict and a combined context string from the LLM response.

        Expects the tag-based output format produced by COMPONENT_VERIFY_PROMPT
        (<verdict>, <error_description>, <gap_filling>, <cited_result_audits>,
        <web_search>). Returns (verdict, combined) where `combined` is a
        readable concatenation of the parsed fields. For INCORRECT verdicts,
        falls back to the full raw output if no error_description was parsed,
        so we never lose information.
        """
        def _tag(name: str, text: str) -> Optional[str]:
            m = re.search(rf'<{name}>(.*?)</{name}>', text, re.DOTALL)
            if not m:
                return None
            val = m.group(1).strip()
            return val if val else None

        def _all_tags(name: str, text: str) -> List[str]:
            return [m.group(1) for m in re.finditer(rf'<{name}>(.*?)</{name}>', text, re.DOTALL)]

        verdict = None
        error_desc = None
        gap_filling = None
        cited_result_audits: List[Dict[str, Any]] = []
        web_search: List[Dict[str, Any]] = []

        v_raw = _tag("verdict", output)
        if v_raw:
            v = v_raw.upper()
            if v in ("CORRECT", "INCORRECT"):
                verdict = v
            else:
                print(f"[_parse_verdict] <verdict> tag had unexpected value: {v_raw!r}")
        else:
            print("[_parse_verdict] no <verdict> tag found in output")

        if verdict is not None:
            error_desc = _tag("error_description", output)
            gap_filling = _tag("gap_filling", output)

            audits_blob = _tag("cited_result_audits", output)
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

            ws_blob = _tag("web_search", output)
            if ws_blob:
                for cit_text in _all_tags("citation", ws_blob):
                    web_search.append({
                        "cited_as": _tag("cited_as", cit_text),
                        "found": _tag("found", cit_text),
                        "source_url": _tag("source_url", cit_text),
                        "statement": _tag("statement", cit_text),
                    })

        # Fallback: bare-word verdict anywhere in output
        if verdict is None:
            m = re.search(r'\b(CORRECT|INCORRECT)\b', output)
            if m:
                verdict = m.group(1)
                print(f"[_parse_verdict] recovered verdict via bare-word fallback: {verdict}")

        # Fallback: #CORRECT/#INCORRECT
        if verdict is None:
            if '#CORRECT' in output and '#INCORRECT' not in output:
                verdict = "CORRECT"
                print("[_parse_verdict] recovered verdict via #CORRECT hashtag fallback")

        # Default to INCORRECT if nothing matched
        if verdict is None:
            verdict = "INCORRECT"
            print(f"[_parse_verdict] no verdict found anywhere; defaulting to INCORRECT. raw output (first 500 chars):\n{output[:500]}")

        # For INCORRECT verdicts, always ensure we have some explanation.
        if verdict == "INCORRECT" and not error_desc:
            error_desc = output

        parts: List[str] = []
        if error_desc:
            parts.append(error_desc)
        if gap_filling:
            parts.append(f"[Gap filling]\n{gap_filling}")
        if cited_result_audits:
            lines = ["[Cited result audits]"]
            for e in cited_result_audits:
                lines.append(f"- cited_as: {e.get('cited_as')}")
                for h in e.get("hypotheses", []):
                    lines.append(f"  - hypothesis: {h.get('hypothesis')}")
                    lines.append(f"    satisfied: {h.get('satisfied')}")
                    lines.append(f"    justification: {h.get('justification')}")
            parts.append("\n".join(lines))
        if web_search:
            lines = ["[Web search results]"]
            for e in web_search:
                lines.append(f"- cited_as: {e.get('cited_as')}")
                lines.append(f"  found: {e.get('found')}")
                lines.append(f"  source_url: {e.get('source_url')}")
                lines.append(f"  statement: {e.get('statement')}")
            parts.append("\n".join(lines))
        combined = "\n\n".join(parts) if parts else None

        return verdict, combined

    async def _check_faithfulness(
        self,
        problem: str,
        original_proof: str,
        decomposition: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Check every component of the rewrite for faithfulness against the original.

        Uses the same context/established-results structure as component
        verification, but with the faithfulness prompt instead.
        Supports 4 levels: propositions, lemmas, claims, facts.
        """
        async def _faith_check(label, statement, proof, context_parts, established_parts):
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

        deps_map: Dict[str, List[str]] = decomposition.get("deps", {})

        def _augment_est_with_deps(est: List[str], proof_id: str) -> None:
            """Append statements of DEPS-cited blocks to *est* (in-place, deduped)."""
            seen = set(est)
            for cited_id in deps_map.get(proof_id, []):
                formatted = _format_block_for_established(decomposition, cited_id)
                if formatted is not None and formatted not in seen:
                    est.append(formatted)
                    seen.add(formatted)

        # ── Facts ───────────────────────────────────────────────────
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    for f_num, fact in sorted(claim.get("facts", {}).items()):
                        ctx = [
                            f"Theorem: {decomposition['theorem']['statement']}",
                            f"Proposition {p_num}: {prop['statement']}",
                            f"Lemma {p_num}.{l_num}: {lemma['statement']}",
                            f"Claim {p_num}.{l_num}.{c_num}: {claim['statement']}",
                        ]
                        est = []
                        for prev_num, prev_prop in sorted_propositions:
                            if prev_num < p_num:
                                est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
                        for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                            if prev_l < l_num:
                                est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                        for prev_c, prev_claim in sorted(lemma.get("claims", {}).items()):
                            if prev_c < c_num:
                                est.append(f"Claim {p_num}.{l_num}.{prev_c}: {prev_claim['statement']}")
                        for prev_f, prev_fact in sorted(claim.get("facts", {}).items()):
                            if prev_f < f_num:
                                est.append(f"Fact {p_num}.{l_num}.{c_num}.{prev_f}: {prev_fact['statement']}")
                        _augment_est_with_deps(est, f"{p_num}.{l_num}.{c_num}.{f_num}")
                        faith_tasks.append((f"fact_{p_num}_{l_num}_{c_num}_{f_num}", _faith_check(
                            f"Fact {p_num}.{l_num}.{c_num}.{f_num}", fact["statement"], fact["proof"], ctx, est
                        )))

        # ── Claims ──────────────────────────────────────────────────
        for p_num, prop in sorted_propositions:
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    ctx = [
                        f"Theorem: {decomposition['theorem']['statement']}",
                        f"Proposition {p_num}: {prop['statement']}",
                        f"Lemma {p_num}.{l_num}: {lemma['statement']}",
                    ]
                    est = []
                    for prev_num, prev_prop in sorted_propositions:
                        if prev_num < p_num:
                            est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
                    for prev_l, prev_lemma in sorted(prop.get("lemmas", {}).items()):
                        if prev_l < l_num:
                            est.append(f"Lemma {p_num}.{prev_l}: {prev_lemma['statement']}")
                    for prev_c, prev_claim in sorted(lemma.get("claims", {}).items()):
                        if prev_c < c_num:
                            est.append(f"Claim {p_num}.{l_num}.{prev_c}: {prev_claim['statement']}")
                    for f_num, fact in sorted(claim.get("facts", {}).items()):
                        est.append(f"Fact {p_num}.{l_num}.{c_num}.{f_num}: {fact['statement']}")
                    _augment_est_with_deps(est, f"{p_num}.{l_num}.{c_num}")
                    faith_tasks.append((f"claim_{p_num}_{l_num}_{c_num}", _faith_check(
                        f"Claim {p_num}.{l_num}.{c_num}", claim["statement"], claim["proof"], ctx, est
                    )))

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
                for c_num, claim in sorted(lemma.get("claims", {}).items()):
                    est.append(f"Claim {p_num}.{l_num}.{c_num}: {claim['statement']}")
                _augment_est_with_deps(est, f"{p_num}.{l_num}")
                faith_tasks.append((f"lemma_{p_num}_{l_num}", _faith_check(
                    f"Lemma {p_num}.{l_num}", lemma["statement"], lemma["proof"], ctx, est
                )))

        # ── Propositions ────────────────────────────────────────────
        for p_num, prop in sorted_propositions:
            ctx = [f"Theorem: {decomposition['theorem']['statement']}"]
            est = []
            for prev_num, prev_prop in sorted_propositions:
                if prev_num < p_num:
                    est.append(f"Proposition {prev_num}: {prev_prop['statement']}")
            for l_num, lemma in sorted(prop.get("lemmas", {}).items()):
                est.append(f"Lemma {p_num}.{l_num}: {lemma['statement']}")
            _augment_est_with_deps(est, f"{p_num}")
            faith_tasks.append((f"proposition_{p_num}", _faith_check(
                f"Proposition {p_num}", prop["statement"], prop["proof"], ctx, est
            )))

        # ── Theorem ─────────────────────────────────────────────────
        est = [f"Proposition {p_num}: {prop['statement']}" for p_num, prop in sorted_propositions]
        _augment_est_with_deps(est, "theorem")
        faith_tasks.append(("theorem", _faith_check(
            "Theorem", decomposition["theorem"]["statement"],
            decomposition["theorem"]["proof"], [], est
        )))

        # Fire all faithfulness checks in parallel
        keys = [k for k, _ in faith_tasks]
        vals = await asyncio.gather(*(coro for _, coro in faith_tasks))
        return dict(zip(keys, vals))

    async def async_completion(
        self,
        prompt: str,
        system_prompt: str = "",
        is_websearch: bool = False,
    ) -> Tuple[str, Dict[str, Any]]:
        tokens_used: Dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
            "final_output_tokens": 0,
        }
        # if 'mini' in self.model :
        #     max_tokens = 25000
        # else :
        #     max_tokens = 50000

        for attempt in range(self.max_tries):
            try:
                start = time.perf_counter()
                extra_kwargs = {}
                if is_websearch:
                    extra_kwargs["tools"] = [{"type": "web_search"}]
                    extra_kwargs["tool_choice"] = "auto"
                resp = await self.client.responses.create(
                    model=self.model,
                    instructions=system_prompt or None,
                    input=[{"role": "user", "content": prompt}],
                    text={"format": {"type": "text"}},
                    max_output_tokens=self.max_tokens,
                    reasoning={"effort": self.effort},
                    **extra_kwargs,
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
                        "timed out",
                        "temporarily",
                        "overloaded",
                        " 5",
                    )
                )
                if attempt == self.max_tries - 1 or not retryable:
                    if attempt == self.max_tries - 1:
                        print(
                            f"⚠ async_completion returning empty output after "
                            f"{self.max_tries} attempts [model={self.model}; "
                            f"websearch={is_websearch}]: "
                            f"{type(e).__name__}: {str(e)[:200]}"
                        )
                    return "", tokens_used
                await asyncio.sleep(self.base * (2**attempt))