"""Prompts for ArxivComplexPseudoFormalisationVerifier.

Minimal-diff variants of the IMO ``complex_prompts`` adapted for arxiv papers:

- The rewrite prompt accepts a whole paper (PDF + LaTeX) and supports
  multiple top-level theorems (id="N"). DEPS for theorem proofs are
  written as ``id="theorem_N"``.
- The component-verify prompt strips the web-search instruction and the
  ``<web_search>`` output block (web search is disabled for arxiv to
  avoid the model self-locating the paper online).
- The faithfulness prompt is the existing
  ``ARXIV_COMPONENT_FAITHFULNESS_PROMPT`` from ``src.verifier.prompts``,
  re-exported for convenience.
- The global block check prompt is reused unchanged from
  ``src.verifier.complex_prompts``.
- The meta-verify prompt is rewritten substantially: the calibrator's role
  shifts from emitting a 0-7 score to emitting an XML error list with
  PDF-rendered locations (matching the arxiv baseline output schema).
"""

from src.verifier.prompts import ARXIV_COMPONENT_FAITHFULNESS_PROMPT  # noqa: F401
from src.verifier.complex_prompts import GLOBAL_BLOCK_CHECK_PROMPT  # noqa: F401


# =============================================================================
# Rewrite prompt — multi-theorem, deep 4-layer, DEPS, PDF + tex inputs
# =============================================================================

ARXIV_COMPLEX_REWRITE_PROMPT = """Rewrite the mathematical paper below into a structured formal proof outline.

You are given:
1. The rendered PDF of the paper (attached as a separate file). Use it to read the paper as a human would and to identify the rendered numerical/letter labels of every theorem/lemma/proposition/corollary/claim.
2. The raw LaTeX source of the paper (included below). Use it for precise notation, equations, and the exact wording of statements and proofs.

Structure:
- The TOP level of the rewrite is a list of **theorems**. Use one ``<THEOREM_STATEMENT id="N">`` block per top-level result the paper announces (its flagship theorem(s) plus any independent secondary theorems / propositions / corollaries that the paper states as headline results — i.e. results that are not used merely as intermediate steps for another result). Number theorems sequentially: id="1", id="2", ... in the order they appear in the paper.
- Below the theorems, propositions form a single SHARED pool numbered globally (1, 2, 3, ...) — they are NOT scoped to a specific theorem; any theorem proof may cite any proposition from this pool.
- The proof tree has up to 5 levels, in this fixed order from the top: (1) Theorem → (2) Proposition → (3) Lemma → (4) Claim → (5) Fact. Always decompose using **consecutive levels starting from the top**. If you use 3 levels, use Theorem + Proposition + Lemma (NOT Theorem + Proposition + Claim — that skips Lemma). If you use 4 levels, use Theorem + Proposition + Lemma + Claim. If you use the maximum 5 levels, use all of Theorem + Proposition + Lemma + Claim + Fact. NEVER skip an intermediate level.
- Within the depth you have chosen, individual blocks at the deepest used level can be leaves (statement + proof, no sub-blocks). For example, when using 4 levels, some claims can be leaves (no facts under them) while other claims have facts. The constraint is only on which TYPES of blocks may appear, not on whether every branch reaches the deepest type.
- The dotted-id depth MUST match the tag: 1 dot-part → PROPOSITION_STATEMENT (id="K"), 2 parts → LEMMA_STATEMENT (id="K.L"), 3 parts → CLAIM_STATEMENT (id="K.L.C"), 4 parts → FACT_STATEMENT (id="K.L.C.F"). Never use a tag with a dotted-id of the wrong depth (e.g. do NOT emit `<CLAIM_STATEMENT id="2.1">` to mean "claim 1 of proposition 2" — that conflicts with lemma id "2.1"). If you want a sub-block of a proposition without an intermediate lemma layer, do not decompose at all and keep the argument inline in the PROPOSITION_PROOF.
- Do not introduce any deeper hierarchy than 4 levels below the theorem.
- If a deeper tree seems natural, flatten it into a sequential list of facts inside the relevant claim.
- Citation scope:
    - A non-theorem proof block (proposition / lemma / claim / fact) may cite only (a) its own direct children (the level immediately below it), and (b) any block declared earlier in the document that lies outside the current block's subtree, **excluding the THEOREM_STATEMENT blocks** (a non-theorem block may not cite a theorem statement).
    - A theorem proof (THEOREM_PROOF id="N") may cite (a) any proposition from the shared pool, and (b) any earlier theorem (id < N) by its statement.
    - A block must NOT cite its own ancestors, its descendants beyond direct children, any block from a later-declared subtree, or itself.
- Citations are statement-only: only the cited block's statement is available as a premise — its proof body is not. If you need an object or intermediate result from inside another block's *_PROOF, hoist it into its own block's statement and cite that.
- Dependency declaration: after every *_PROOF block, emit a ``<DEPS id="...">`` tag listing the ids of all blocks actually cited in that proof, comma-separated. Every id referenced in the proof text must appear in DEPS, and every id in DEPS must obey the citation scope above. If the proof cites nothing, emit an empty tag: ``<DEPS id="..."></DEPS>``.
    - For non-theorem proofs, the DEPS id is the SAME id as the proof it follows (e.g. ``<DEPS id="1.1.1">`` for ``<CLAIM_PROOF id="1.1.1">``). The body lists comma-separated dotted ids of cited blocks (no theorem references).
    - For theorem proofs, the DEPS id is ``"theorem_N"`` where N is the theorem id (e.g. ``<DEPS id="theorem_2">`` for ``<THEOREM_PROOF id="2">``). The body may contain (a) bare integer ids for cited propositions (and their descendants by dotted id, if any are needed), and (b) ``"theorem_M"`` references for any earlier theorem (M < N) the proof relies on. Example: ``<DEPS id="theorem_2">1, 3, theorem_1</DEPS>`` means Theorem 2's proof cites Propositions 1 and 3, plus Theorem 1's statement. This naming convention avoids collisions with proposition DEPS.
- No trivial decompositions: do not decompose a theorem into a single proposition that restates the theorem, nor a proposition into a single lemma that restates the proposition, nor a lemma into a single claim that restates the lemma, nor a claim into a single fact that restates the claim.
- If a theorem proof contains several distinct assertions or sub-arguments, decompose it into multiple propositions.
- If a proposition proof contains several distinct assertions or sub-arguments, decompose it into multiple lemmas.
- If a lemma proof contains several distinct assertions or sub-arguments, decompose it into multiple claims.
- If a claim proof contains several distinct assertions or sub-arguments, decompose it into multiple facts.
- For challenging proofs, decompose based on the nature of the argument: arguments of different kinds (combinatorial vs. algebraic vs. inductive vs. case analysis) should be separated into different claims or facts; and if only a subset of the constraints/hypotheses is needed to justify a step in the proof block, that step probably should be a separate unit.
- A *_PROOF block must not restate or paraphrase its own statement. The core assertion of a block goes in its statement; the *_PROOF holds only the justification, method, or new setup that is not already in the statement. If the only thing you would write in *_PROOF is a paraphrase of the statement, write exactly None instead — "None" is legitimate proof.
- Preserve every external citation from the original paper (references to prior papers, named theorems, etc.). At the end of each *_PROOF block that uses such citations, list those external results. If the original paper provides a proper citation with author name, link, and journal, provide them as well. External citations are separate from DEPS, which only tracks internal block-to-block references.

Numbering and order:
- Number theorems sequentially: id="1", id="2", ... in document order.
- Number propositions sequentially across the whole paper (single shared pool, NOT per-theorem): 1, 2, 3, ...
- Number lemmas within each proposition: 1.1, 1.2, ..., 2.1, 2.2, ...
- Number claims within each lemma: 1.1.1, 1.1.2, ... 1.2.1, 1.2.2, ... 2.1.1, 2.1.2
- Number facts within each claim: 1.1.1.1, 1.1.1.2, ... 1.1.2.1, 1.1.2.2, ... 2.1.1.1, 2.1.1.2, ...
- The proof must read top-to-bottom as a forward sequence: if component j uses component i at the same level, then j > i. Reorder to avoid forward references.

Faithfulness to the original paper:
- Preserve the paper's mathematical content, notation, logical flow, ordering, and wording as much as possible.
- For leaf components (facts, claims with no sub-facts and lemmas with no sub-claims), keep the original wording verbatim wherever possible; only edit when required by the structural constraints above.
- Only make the minimal edits needed to fit the structured format.
- Do not introduce alternative arguments, and do not repair, optimize, strengthen, or silently fix the paper.
- Do not add justifications absent from the original paper, omit relevant proof details, or introduce statements stronger than what the original paper establishes.
- Preserve the original's sequential reasoning. If the original derives steps in order, do not merge them into a joint or parallel deduction — that can hide errors that only appear step by step.

Assumptions, conditions, and definitions:
- Clearly state the assumptions, conditions, and definitions for every theorem, proposition, lemma, claim and fact.
- Each component may inherit the setting of its enclosing parent, but if tracing back through multiple earlier statements would be needed, restate the relevant assumptions explicitly.
- If a component modifies its parent's setting, explicitly state the full updated assumptions and note which assumptions were added, removed, or changed relative to the parent.

Output format:
Wrap every section in XML-style delimiter tags as shown in the template below.
- Use EXACTLY the tag names shown: THEOREM_STATEMENT, PROPOSITION_STATEMENT, LEMMA_STATEMENT, CLAIM_STATEMENT, FACT_STATEMENT, FACT_PROOF, CLAIM_PROOF, LEMMA_PROOF, PROPOSITION_PROOF, THEOREM_PROOF, DEPS.
- Every tag MUST have an id attribute matching the numbering scheme above (including THEOREM_STATEMENT and THEOREM_PROOF — multi-theorem mode). DEPS for THEOREM_PROOF carries id="theorem_N" as noted above.
- Do NOT nest tags inside each other. All tags are at the top level.
- Do NOT include any text outside of tags.
- Include all assumptions, conditions, and definitions INSIDE the statement tag they belong to.
- Use LaTeX notation for all mathematical notations.

Template (single-theorem case shown for compactness; emit additional ``<THEOREM_STATEMENT id="N">`` and ``<THEOREM_PROOF id="N">`` blocks when the paper has multiple top-level theorems):

<THEOREM_STATEMENT id="1">
Assumptions / Conditions / Definitions.
- ...
Statement :
...
</THEOREM_STATEMENT>

<THEOREM_STATEMENT id="2">
...
</THEOREM_STATEMENT>

<PROPOSITION_STATEMENT id="1">
Assumptions / Conditions / Definitions.
- ...
Statement :
...
</PROPOSITION_STATEMENT>

<LEMMA_STATEMENT id="1.1">
Assumptions / Conditions / Definitions.
- ...
Statement :
...
</LEMMA_STATEMENT>

<CLAIM_STATEMENT id="1.1.1">
Assumptions / Conditions / Definitions.
- ...
Statement :
...
</CLAIM_STATEMENT>

<FACT_STATEMENT id="1.1.1.1">
Assumptions / Conditions / Definitions.
- ...
Statement :
...
</FACT_STATEMENT>

<FACT_PROOF id="1.1.1.1">
[proof.]
</FACT_PROOF>
<DEPS id="1.1.1.1">[comma-separated ids of blocks cited in this fact proof, or empty]</DEPS>

<CLAIM_PROOF id="1.1.1">
[explain how Facts 1.1.1.1, 1.1.1.2, ... imply Claim 1.1.1.]
</CLAIM_PROOF>
<DEPS id="1.1.1">1.1.1.1, 1.1.1.2, ...</DEPS>

<LEMMA_PROOF id="1.1">
[explain how Claims 1.1.1, 1.1.2, ... imply Lemma 1.1.]
</LEMMA_PROOF>
<DEPS id="1.1">1.1.1, 1.1.2, ...</DEPS>

<PROPOSITION_PROOF id="1">
[explain how Lemmas 1.1, 1.2 imply Proposition 1.]
</PROPOSITION_PROOF>
<DEPS id="1">1.1, 1.2, ...</DEPS>

<PROPOSITION_STATEMENT id="2">
...
</PROPOSITION_STATEMENT>

...

<THEOREM_PROOF id="1">
[explain how Propositions 1, 3, ... imply Theorem 1.]
</THEOREM_PROOF>
<DEPS id="theorem_1">1, 3, ...</DEPS>

<THEOREM_PROOF id="2">
[explain how Propositions 2, 4, ... imply Theorem 2.]
</THEOREM_PROOF>
<DEPS id="theorem_2">2, 4, ...</DEPS>


Now rewrite the following paper in this format. The PDF is attached as a separate file; the raw LaTeX source is below.

LATEX SOURCE:
{paper_tex}
"""


# =============================================================================
# Regenerate prompt — same shape as IMO complex regenerate, but the input is a
# whole paper (single {original_paper} field, plus PDF attached).
# =============================================================================

ARXIV_COMPLEX_REGENERATE_REWRITE_PROMPT = """You previously produced a structured rewrite of a mathematical paper, but a faithfulness checker identified discrepancies between your rewrite and the original paper.

Your task is to produce a NEW rewritten paper that fixes the identified issues while still following the structured format.

You will receive:
1. The rendered PDF of the paper (attached as a separate file).
2. The raw LaTeX source of the paper (included below — ground truth — your rewrite must faithfully represent this).
3. Your previous rewritten paper, which contained faithfulness errors.
4. The specific discrepancies flagged by the checker.

Requirements for the new rewrite:
- Fix every issue listed in the Identified Errors. For each issue, make sure the new rewrite no longer deviates from the original paper in that way.
- Do NOT introduce new discrepancies: do not strengthen/weaken claims, omit steps, add arguments, drift in notation, or misrepresent the logical structure relative to the Original Paper.
- Preserve the original paper's content, notation, logical flow, ordering, and wording as much as possible.
- Follow the SAME structured output format (XML-style tags, multi-theorem schema, DEPS conventions, etc.) as the rewriting instructions below.
- **Use consecutive levels of decomposition starting from the top** (Theorem → Proposition → Lemma → Claim → Fact). Never skip an intermediate level. Match the tag to the dotted-id depth strictly: 1-dot id is a PROPOSITION, 2-dot is a LEMMA, 3-dot is a CLAIM, 4-dot is a FACT. Do not, for example, emit `<CLAIM_STATEMENT id="2.1">` (that would conflict with lemma id "2.1").

--- Original rewriting instructions (follow these for the output format) ---
{rewrite_instructions}
--- End of rewriting instructions ---

ORIGINAL LATEX SOURCE:
{original_paper}

PREVIOUS REWRITTEN PAPER (contains errors):
{previous_rewrite}

IDENTIFIED ERRORS:
{errors}

Now produce the corrected rewritten paper. Output ONLY the rewritten paper using the XML-tag format — no commentary before or after.
"""


# =============================================================================
# Component-verify prompt — verbatim copy of complex_prompts.COMPONENT_VERIFY_PROMPT
# with two surgical edits:
#  (a) the paragraph instructing the model to perform a web search for external
#      citations is removed (web search is disabled for arxiv);
#  (b) the ``<web_search>`` output block is removed from both CORRECT and
#      INCORRECT output templates.
# Everything else (gap_filling, cited_result_audits, conservative-flagging
# instructions, hypothesis audit conventions) is kept unchanged.
# =============================================================================

ARXIV_COMPLEX_COMPONENT_VERIFY_PROMPT = """You are an expert mathematical proof verifier specialized in research-level mathematics.

Your task is to verify whether the proposed proof of a specific statement, called "Assertion", is correct.

You are given:
1. **Contexts**: A sequence of statements from which the Assertion may or may not inherit definitions, assumptions, or conditions. These are often the parent or ancestor statements of the Assertion, and can be the same as the global theorem. They are provided solely so you can understand the definitions and assumptions of the Assertion. They have NOT been verified and may be incorrect. Do not treat them as established truths, and do not verify them yourself. Also do not automatically assume that the Assertion inherits assumptions or definitions from them. The Assertion will specify which settings or assumptions it inherits from these contextual statements.
2. **Established Results**: Statements that have already been verified or can be assumed to be correct. You may assume all established results are correct and use them freely — do NOT re-verify them. The proof of the Assertion can invoke these results as long as the assumptions are properly justified and the definitions are consistent.
3. **Assertion**: The specific statement whose proof you must verify.
4. **Proposed Proof**: The proof of the Assertion to verify.

Instructions:
- Verify ONLY the proposed proof of the Assertion.
- Read the Assertion carefully and analyze the proof step by step.
- Identify any incorrect, or logically invalid reasoning.

- When the proof references an established result, you may trust its conclusion, but you must verify that it is correctly applied:
    - Check that the result is used within its valid scope.
    - Explicitly identify the assumptions of the referenced result and confirm that each one is satisfied in the current context.
    - Verify that the definitions used in the invoked established results are the same as in the Assertion.
    - Detail which assumptions hold and why.
   - If the proof misapplies an established result, the error description must name which assumption failed to hold or which definition diverged from the Assertion's usage.
   - The proof is not required to restate the assumptions of a cited result. However, you must explicitly audit every use of a cited result: list all of its hypotheses and confirm, one by one, where each is satisfied in the current context. If any hypothesis is not actually satisfied, you must return INCORRECT for misapplication.

- Do NOT flag a step as incorrect merely because it omits intermediate justification, nor because it contains a local slip that a careful reader can repair on the spot.
Your job is to detect genuine errors — load-bearing false claims, misapplied results, logical invalidity, inconsistent use of terms — NOT to demand that every step be spelled out. Terseness, skipped arithmetic, standard manipulations, routine verifications, and minor slips are not errors when the intended correct statement is unambiguous from context and the rest of the proof still goes through; multiple such gaps do not compound into one.
  Before flagging, try to fill the gap or repair the slip yourself using the Contexts, the Established Results, and standard mathematical knowledge appropriate to the problem's level. Flag only when (a) the mistake is load-bearing (the Assertion's conclusion or a later step genuinely depends on the incorrect claim), or (b) the repair would require a substantive new idea, a nontrivial result, or a definition not available in the given material. In case (b), wrap each missing result in a <lemma> tag and each missing term in a <definition> tag — one tag per missing item:
  <lemma>full precise statement, including hypotheses and conclusion</lemma>
  <definition>term: full unambiguous definition</definition>
  A repair must not change the Assertion's hypotheses or conclusion: if fixing the proof would require adding an assumption, restricting the domain, or weakening the target, return INCORRECT.

- A Proposed Proof of exactly "None" is legitimate: treat it as an empty proof and apply the gap-filling test above — CORRECT if the Assertion is fillable from Contexts, Established Results, and standard knowledge; otherwise INCORRECT.

- When you DID fill gaps or repair minor slips yourself to reach CORRECT, record inside `<gap_filling>` what was missing or misstated and the reasoning used — enough that a reviewer could verify the step. Leave `<gap_filling>` empty when the proof was complete and error-free as written, or when the verdict is INCORRECT.

- Record your hypothesis audit of every cited result inside the `<cited_result_audits>` block — one `<audit>` entry per use of a cited result. Use this whenever the proof cites a result, even when the proof was complete as written and `<gap_filling>` is empty. Leave `<cited_result_audits>` empty ONLY when the proof cites no results at all. This requirement applies equally to CORRECT and INCORRECT verdicts.

At the very end of your response, you MUST output your final verdict using the tag format below. Do NOT write anything after the closing `</cited_result_audits>` tag. Inside any tag's text content you may write LaTeX freely — backslashes, braces, `<`, and `>` need NO escaping. The only requirement is that every opening tag has a matching closing tag exactly as shown.

If CORRECT, output:
<verdict>CORRECT</verdict>
<error_description></error_description>
<gap_filling>
<for each gap closed or slip repaired: what was missing or misstated and the reasoning used — concise but auditable; leave empty if the proof was complete and error-free as written>
</gap_filling>
<cited_result_audits>
<audit>
<cited_as><exactly what the proof wrote></cited_as>
<hypothesis>
<statement><the cited result's hypothesis></statement>
<satisfied>true</satisfied>
<justification><concrete reason it holds in the current context></justification>
</hypothesis>
</audit>
</cited_result_audits>

If INCORRECT, output:
<verdict>INCORRECT</verdict>
<error_description>
Identify the specific step that fails, state what it claims, and explain why it is wrong or unjustified.
</error_description>
<gap_filling></gap_filling>
<cited_result_audits>
<audit>
<cited_as><exactly what the proof wrote></cited_as>
<hypothesis>
<statement><the cited result's hypothesis></statement>
<satisfied>true_or_false</satisfied>
<justification><concrete reason it holds or fails in the current context></justification>
</hypothesis>
</audit>
</cited_result_audits>

**CONTEXTS**

{contexts}

**ESTABLISHED RESULTS**

{established_results}

**ASSERTION**

{assertion}

**PROPOSED PROOF**

{proof}

"""


# =============================================================================
# Meta-verify (calibrator) — substantially rewritten relative to the IMO
# complex META_VERIFY_PROMPT.
#
# Role on arxiv: take the PDF + tex + rewritten paper + the per-component
# error reports flagged by the block verifier, decide which flagged errors are
# genuine errors in the underlying paper (not rewrite artifacts), optionally
# add other errors observable from the PDF that components missed, and emit
# the final list as the same XML format used by ArxivVerifierBaseline.
# Crucially, location strings MUST use the rendered PDF labels (e.g.
# "Theorem 19", "Lemma 4.3"), not the rewrite-tree internal labels (which
# carry no relation to the paper's own numbering).
# =============================================================================

ARXIV_COMPLEX_META_VERIFY_PROMPT = """You are an expert mathematical referee. Your task is to produce the FINAL list of mathematical errors in a peer-reviewed mathematics paper.

You are given:
1. The rendered PDF of the original paper (attached as a file). This is your authoritative source for the labels you must use in the final answer (e.g. "Theorem 19", "Lemma 4.3", "Proposition 5.1", "Theorem B").
2. The raw LaTeX source of the paper (included below). Use this for precise notation, equations, and exact wording.
3. A structured rewrite of the paper, decomposed into theorems / propositions / lemmas / claims / facts. The rewrite was produced by an automated rewriter and may itself introduce mistakes, omissions, or distortions that were NOT present in the original paper.
4. A list of potential errors that an automated component-verifier flagged in specific components of the rewritten paper. These potential errors may or may not be genuine errors in the underlying paper — in particular, an "error" may be an artifact of the rewriting process (e.g., the rewriter dropped a key step, misstated a claim, or restructured the argument in a way that obscures correct reasoning that IS present in the original paper).

Evaluation process:

1. **Error validation.** For each potential error, carefully determine whether it is a genuine error in the underlying paper or a false alarm. Examine the error in the context of the full rewritten paper, AND cross-check against the original paper (PDF + LaTeX) to see whether the reasoning the rewritten paper is missing or misstating actually appears (correctly) in the original. If so, treat the error as a rewriting artifact rather than a genuine error in the paper.

2. **Search for additional errors.** You may also report errors in the original paper that the component verifier did NOT flag — including errors in unlabelled prose passages, between numbered results, or that span multiple components. Do not feel constrained to only the listed potential errors.

3. **Report only mathematical errors.** Focus on errors of mathematical substance (incorrect proofs, unjustified steps, false claims, gaps in reasoning, miscomputed quantities, misapplied theorems). Do not report typos, stylistic concerns, formatting issues, or notational preferences.

CRITICAL — labelling of locations in the final answer:

For every error you report, the `<location>` field MUST use the rendered label exactly as it appears IN THE PDF. Examples: "Theorem 19", "Lemma 4.3", "Proposition 5.1", "Corollary 3.5", "Theorem B".

Do NOT use:
- Internal rewrite-tree labels like "Proposition 7", "Claim 1.1.1", or "Fact 2.3.1.1" (those are the rewriter's invented numbering and do not match the paper's numbering).
- Raw LaTeX `\\ref{{...}}` or `\\label{{...}}` references.

If the error is in an unlabelled passage, use a short descriptive locator a reader can find in the PDF (for example "Section 3.2, paragraph after Definition 2.1").

OUTPUT FORMAT:

Output your final answer using the following XML-style format. Use one `<error>` block per error. Use the field tags exactly as shown. You may write LaTeX math (with raw backslashes) freely inside the `<description>` field; do not escape anything. Do not output anything after the closing `</errors>` tag.

<errors>
  <error>
    <location>Theorem 4.2</location>
    <description>Brief description of the mathematical error and why it is wrong.</description>
  </error>
  <error>
    <location>Lemma 5.1</location>
    <description>...</description>
  </error>
</errors>

If you find no genuine mathematical errors in the paper, output an empty errors block:

<errors>
</errors>

LATEX SOURCE OF ORIGINAL PAPER:
{original_paper}

REWRITTEN PAPER (PROPOSED STRUCTURED FORM):
{rewritten_paper}

POTENTIAL ERRORS (from automated verification of the rewritten paper):
{errors}
"""
