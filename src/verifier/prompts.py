REFERENCE_PROMPT = """You are an expert grader for the International Mathematics Olympiad (IMO).
Your task is to evaluate a proposed solution strictly and rigorously.

Keep in mind the standards at the IMO are extremely high: only arguments that are logically sound, complete, and precise should be rewarded.

### General Scoring Rubric

Scores are assigned on a 0–7 scale. The general guidelines are:

* **7 Points (Correct):**
  The solution is complete, correct, and fully rigorous.
  If the submission contains incorrect attempts or lines of reasoning
  but ultimately presents a complete and correct solution, it should
  still be awarded full points; the presence of earlier, discarded work
  does not detract from the final correct proof.

* **6 Points (Almost Correct):**
  The solution is almost correct with a sound core argument, but contains
  minor errors in calculation or small gaps in logic. Missing proofs for
  major components, unjustified claims, or sketchy arguments are **not**
  eligible for 6 points.

* **1 Point (Partial Progress):**
  The solution demonstrates substantial progress explicitly mentioned in
  the grading guidelines. Initial observations, reformulating the problem
  without making substantive headway, or proving partial results not
  mentioned in the grading guidelines are generally **not** eligible for
  this score.

* **0 Points (Incorrect):**
  The solution does not make substantial progress that is a key step in
  the full solution or is fundamentally flawed. All partial progress
  without key results or lacking rigor also fall in this category.

### Input Data and Interpretation

You are provided with the following:

1. **Problem Statement:**  
   The IMO problem.

2. **Ground Truth Solution:**  
   A reference solution. Assume this solution is correct. It demonstrates one valid approach.

3. **Specific Grading Guidelines:**  
   Criteria for awarding credit for this specific problem. These
   guidelines take precedence over the General Scoring Rubric, especially
   for partial credit.

4. **Proposed Solution:**  
   The student submission.

### Evaluation Process

You must follow this structured process:

1. **Analyze References:**  
   Meticulously read and understand the problem and Ground Truth Solution.
   Check the Specific Grading Guidelines. Identify the key steps for a
   complete solution and the criteria for partial credit.

2. **Step-by-Step Verification:**  
   Verify the logical validity and rigor of every step. Identify all
   flaws, gaps, assumptions, and errors. **Make sure you fully understand
   every piece of logic behind each step of the proposed solution; be
   careful of solutions that “pretend” to be correct.**

3. **Assess Progress:**  
   Determine the extent of non-trivial progress made.

4. **Score Determination:**  
   Compare the findings against the Specific Grading Guidelines and the
   General Rubric to determine the final score.

### Output Requirements

You must provide your final score in the format:

<points>N out of 7</points>

Ensure the `<points>` block is used **only once**, as your answer will be
parsed based on the first `<points></points>` block that appears in your
entire response.

**PROBLEM STATEMENT**
{problem_statement}

**GROUND-TRUTH SOLUTION**
{solution}

**SPECIFIC GRADING GUIDELINES**
{guidelines}

**PROPOSED SOLUTION**
{student_answer}

Present your detailed thought process and formal justification based on
the scoring rubric and grading guidelines, and finally present your
final score in the format below.

[Select one of the following options]

<points>7 out of 7</points>  
<points>6 out of 7</points>  
<points>1 out of 7</points>  
<points>0 out of 7</points>
"""

WITHOUT_REFERENCE_PROMPT = """You are an expert grader for the International Mathematics Olympiad (IMO).
Your task is to evaluate a proposed solution strictly and rigorously.

Keep in mind the standards at the IMO are extremely high: only arguments that are logically sound, complete, and precise should be rewarded.

### General Scoring Rubric

Scores are assigned on a 0–7 scale. The general guidelines are:

* **7 Points (Correct):**
  The solution is complete, correct, and fully rigorous.
  If the submission contains incorrect attempts or lines of reasoning
  but ultimately presents a complete and correct solution, it should
  still be awarded full points; the presence of earlier, discarded work
  does not detract from the final correct proof.

* **6 Points (Almost Correct):**
  The solution is almost correct with a sound core argument, but contains
  minor errors in calculation or small gaps in logic. Missing proofs for
  major components, unjustified claims, or sketchy arguments are **not**
  eligible for 6 points.

* **1 Point (Partial Progress):**
  The solution demonstrates substantial progress. Initial observations, reformulating the problem
  without making substantive headway are generally **not** eligible for
  this score.

* **0 Points (Incorrect):**
  The solution does not make substantial progress that is a key step in
  the full solution or is fundamentally flawed. All partial progress
  without key results or lacking rigor also fall in this category.

### Input Data and Interpretation

You are provided with the following:

1. **Problem Statement:**
   The IMO problem.

2. **Proposed Solution:**
   The student submission.

### Evaluation Process

You must follow this structured process:

1. **Step-by-Step Verification:**
   Verify the logical validity and rigor of every step. Identify all
   flaws, gaps, assumptions, and errors. **Make sure you fully understand
   every piece of logic behind each step of the proposed solution; be
   careful of solutions that "pretend" to be correct.**

2. **Assess Progress:**
   Determine the extent of non-trivial progress made.

3. **Score Determination:**
   Compare the findings against the Specific Grading Guidelines and the
   General Rubric to determine the final score.

### Output Requirements

You must provide your final score in the format:

<points>N out of 7</points>

Ensure the `<points>` block is used **only once**, as your answer will be
parsed based on the first `<points></points>` block that appears in your
entire response.

**PROBLEM STATEMENT**
{problem_statement}

**PROPOSED SOLUTION**
{student_answer}

Present your detailed thought process and formal justification based on
the scoring rubric and grading guidelines, and finally present your
final score in the format below.

[Select one of the following options]

<points>7 out of 7</points>
<points>6 out of 7</points>
<points>1 out of 7</points>
<points>0 out of 7</points>
"""

COMPONENT_VERIFY_PROMPT = """You are an expert mathematical proof verifier.

Your task is to verify whether the proposed proof of a specific statement, called "Assertion", is correct.

You are given:
1. **Contexts**: A sequence of statements from which the Assertion may or may not inherit definitions, assumptions, or conditions. These are often the parent or ancestor statements of the Assertion, and can be the same as the global theorem. They are provided solely so you can understand the definitions and assumptions of the Assertion. They have NOT been verified and may be incorrect. Do not treat them as established truths, and do not verify them yourself. By default, you can inherit the assumption from the parent statements. The Assertion will specify which settings or assumptions it inherits from these contextual statements.
2. **Established Results**: Statements that have already been verified or can be assumed to be correct. You may assume all established results are correct and use them freely — do NOT re-verify them. The proof of the Assertion can invoke these results as long as the assumptions are properly justified and the definitions are consistent.
3. **Assertion**: The specific statement whose proof you must verify.
4. **Proposed Proof**: The proof of the Assertion to verify.

Instructions:
- Verify ONLY the proposed proof of the Assertion.
- Read the Assertion carefully and analyze the proof step by step.
- Identify any incorrect, unjustified, or logically invalid reasoning.
- Pay close attention to potentially confusing or ambiguous interpretations of concepts.
- When the proof references an established result, you may trust its conclusion, but you must verify that it is correctly applied:
    - Check that the result is used within its valid scope.
    - Explicitly identify the assumptions of the referenced result and confirm that each one is satisfied in the current context.
    - Verify that the definitions used in the invoked established results are the same as in the Assertion.
    - Detail which assumptions hold and why.
- For every term used in the proof, verify that its interpretation is unambiguous and consistent throughout. If a term is used with different meanings in different places, the proof is incorrect — do not guess or resolve the ambiguity yourself.

If the proof is INCORRECT, you must additionally classify the error into exactly **one** of the following three categories:

- **(a) Genuine gap** — a step is missing justification AND the gap cannot be locally repaired with a small amount of work. The step is not obviously true to an expert; closing the gap would require a substantive new argument. This is clearly negative.
- **(b) Acceptable elision** — a step is missing justification BUT the step/assertion itself is correct, and is either obvious to an expert reader or does not require explicit justification. The proof has skipped routine work that does not need to be spelled out.
- **(c) Local repair** — a step contains an incorrect justification, but the step is correct or correctable with a small amount of local work (e.g. fixing a sign, citing the right lemma, or rewriting one short paragraph). The high-level argument survives.

At the very end of your response, you MUST output your final verdict as a JSON block. Do NOT write anything after the JSON block.

If CORRECT:
```json
{{"verdict": "CORRECT", "error_description": null, "error_class": null}}
```

If INCORRECT:
```json
{{"verdict": "INCORRECT", "error_description": "Identify the specific step that fails, state what it claims, and explain why it is wrong or unjustified.", "error_class": "a" | "b" | "c"}}
```

**CONTEXTS**

{contexts}

**ESTABLISHED RESULTS**

{established_results}

**ASSERTION**

{assertion}

**PROPOSED PROOF**

{proof}
"""

COMPONENT_FAITHFULNESS_PROMPT = """You are an expert mathematician reviewing whether a component of a rewritten proof faithfully represents the corresponding part of the original proof.

You are given:
1. **Problem Statement**: The mathematical problem.
2. **Original Proof**: The full original proof.
3. **Contexts**: Statements from the rewritten proof that the Assertion inherits definitions or assumptions from (e.g., the theorem statement, the enclosing proposition statement). These are provided so you can understand the scope of the Assertion.
4. **Established Results**: Statements from the rewritten proof that have already been checked and can be assumed to be faithfully rewritten. You may use them as reference points.
5. **Assertion**: The specific rewritten statement whose faithfulness you must verify.
6. **Proposed Proof**: The rewritten proof of the Assertion.

Your task is to determine whether the Assertion and its Proposed Proof **faithfully represent** the corresponding part of the Original Proof.

Check for:
1. **Strengthened or weakened claims**: Does the Assertion claim more or less than the original proof establishes at the corresponding point?
2. **Omitted content**: Does the Proposed Proof drop a non-trivial argument that appears in the original?
3. **Added content**: Does the Proposed Proof introduce new arguments, repairs, or proof ideas not present in the original?
4. **Notation drift**: Are variables, functions, or definitions used differently than in the original?
5. **Misinterpretation**: Does the Assertion or Proposed Proof misunderstand the original's reasoning or logical structure?
6. **Scope errors**: Are assumptions incorrectly inherited, dropped, or added compared to the original?

Instructions:
- Do NOT judge whether the original proof is mathematically correct. Your sole task is faithfulness.
- Only flag changes that alter mathematical meaning. Cosmetic rephrasing is fine.
- Use the Contexts to understand what the Assertion is allowed to assume.
- Use the Established Results as anchors: if a prior component was faithful, you can compare the current component's references to it.

At the very end of your response, output your verdict as a JSON block. Do NOT write anything after the JSON block.

If FAITHFUL:
```json
{{"verdict": "FAITHFUL", "error_description": null}}
```

If UNFAITHFUL:
```json
{{"verdict": "UNFAITHFUL", "error_description": "Identify the specific discrepancy: what the rewrite says vs. what the original says."}}
```

**PROBLEM STATEMENT**
{problem}

**ORIGINAL PROOF**
{original_proof}

**CONTEXTS**
{contexts}

**ESTABLISHED RESULTS**
{established_results}

**ASSERTION**
{assertion}

**PROPOSED PROOF**
{proof}
"""

REWRITE_PROMPT = """Rewrite the following theorem and proof into a structured formal proof outline.

Structure:
- Use at most 2 layers in the proof tree:
  1. Propositions
  2. Lemmas
- Do not introduce any deeper hierarchy.
- If a deeper tree seems natural, flatten it into a sequential list of lemmas inside the relevant proposition.
- The theorem proof may only cite propositions; a proposition proof may only cite its own lemmas and the statements of earlier propositions.
- No trivial decompositions: do not decompose the theorem into a single proposition that restates the theorem, nor a proposition into a single lemma that restates the proposition.
- Every rewritten assertion must be unambiguous: definitions and statements must be precise, and each variable must have a single, clear meaning within an assertion.


Numbering and order:
- Number propositions sequentially: 1, 2, 3, ...
- Number lemmas within each proposition: 1.1, 1.2, ..., 2.1, 2.2, ...
- The proof must read top-to-bottom as a forward sequence: if component j uses component i at the same level, then j > i. Reorder to avoid forward references.

Faithfulness to the original proof:
- Preserve the proof's content, notation, logical flow, ordering, and wording as much as possible.
- For low-level arguments inside leaf components, avoid changing the original wording.
- Only make the minimal edits needed to fit the structured format.
- Do not introduce alternative arguments, and do not repair, optimize, strengthen, or silently fix the proof.
- Do not add justifications absent from the original proof, omit relevant proof details, or introduce statements stronger than what the original proof establishes.

Assumptions, conditions, and definitions:
- Clearly state the assumptions, conditions, and definitions for every theorem, proposition, and lemma.
- Each component may inherit the setting of its enclosing parent, but if tracing back through multiple earlier statements would be needed, restate the relevant assumptions explicitly.
- If a component modifies its parent's setting, explicitly state the full updated assumptions and note which assumptions were added, removed, or changed relative to the parent.

Output format:
Wrap every section in XML-style delimiter tags as shown in the template below.
- Use EXACTLY the tag names shown: THEOREM_STATEMENT, PROPOSITION_STATEMENT, LEMMA_STATEMENT, LEMMA_PROOF, PROPOSITION_PROOF, THEOREM_PROOF.
- Every tag except THEOREM_STATEMENT and THEOREM_PROOF MUST have an id attribute matching the numbering scheme above.
- Do NOT nest tags inside each other. All tags are at the top level.
- Do NOT include any text outside of tags.
- Include all assumptions, conditions, and definitions INSIDE the statement tag they belong to.
- Use LaTeX notation for all mathematical notations.

Template:

<THEOREM_STATEMENT>
Assumptions / Conditions / Definitions.
- ...
Statement :
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

<LEMMA_PROOF id="1.1">
[proof.]
</LEMMA_PROOF>

<PROPOSITION_PROOF id="1">
[explain how Lemmas 1.1, 1.2 imply Proposition 1.]
</PROPOSITION_PROOF>

<PROPOSITION_STATEMENT id="2">
...
</PROPOSITION_STATEMENT>

...

<THEOREM_PROOF>
[explain how Propositions 1, 2, ... imply the theorem.]
</THEOREM_PROOF>


Now rewrite the following theorem and proof in this format:

[PASTE THEOREM AND PROOF HERE]
"""

REGENERATE_REWRITE_PROMPT = """You previously produced a structured rewrite of a mathematical proof, but a faithfulness checker identified discrepancies between your rewrite and the original proof.

Your task is to produce a NEW rewritten proof that fixes the identified issues while still following the structured format.

You will receive:
1. **Problem Statement**: The mathematical problem.
2. **Original Proof**: The student's original proof (ground truth — your rewrite must faithfully represent this).
3. **Previous Rewritten Proof**: Your earlier attempt, which contained faithfulness errors.
4. **Identified Errors**: The specific discrepancies flagged by the checker.

Requirements for the new rewrite:
- Fix every issue listed in the Identified Errors. For each issue, make sure the new rewrite no longer deviates from the original proof in that way.
- Do NOT introduce new discrepancies: do not strengthen/weaken claims, omit steps, add arguments, drift in notation, or misrepresent the logical structure relative to the Original Proof.
- Preserve the original proof's content, notation, logical flow, ordering, and wording as much as possible.
- Follow the SAME structured output format (XML-style tags, numbering, etc.) as the rewriting instructions below.

--- Original rewriting instructions (follow these for the output format) ---
{rewrite_instructions}
--- End of rewriting instructions ---

**PROBLEM STATEMENT**
{problem}

**ORIGINAL PROOF**
{original_proof}

**PREVIOUS REWRITTEN PROOF (contains errors)**
{previous_rewrite}

**IDENTIFIED ERRORS**
{errors}

Now produce the corrected rewritten proof. Output ONLY the rewritten proof using the XML-tag format — no commentary before or after.
"""

META_VERIFY_PROMPT = """You are an expert grader for the International Mathematics Olympiad (IMO).
Your task is to evaluate **the original proof** strictly and rigorously
and assign it a final score. The rewritten proof and the list of
potential errors are provided only as diagnostic aids — they are **not**
what you are grading.

Keep in mind the standards at the IMO are extremely high: only arguments that are logically sound, complete, and precise should be rewarded.

### General Scoring Rubric

Scores are assigned on a 0–7 scale. The general guidelines are:

* **7 Points (Correct):**
  The solution is complete, correct, and fully rigorous.
  If the submission contains incorrect attempts or lines of reasoning
  but ultimately presents a complete and correct solution, it should
  still be awarded full points; the presence of earlier, discarded work
  does not detract from the final correct proof.

* **6 Points (Almost Correct):**
  The solution is almost correct with a sound core argument, but contains
  minor errors in calculation or small gaps in logic. Missing proofs for
  major components, unjustified claims, or sketchy arguments are **not**
  eligible for 6 points.

* **1 Point (Partial Progress):**
  The solution demonstrates substantial progress. Initial observations,
  reformulating the problem without making substantive headway are
  generally **not** eligible for this score.

* **0 Points (Incorrect):**
  The solution does not make substantial progress that is a key step in
  the full solution or is fundamentally flawed. All partial progress
  without key results or lacking rigor also fall in this category.

### Input Data and Interpretation

You are provided with the following:

1. **Problem Statement:**
   The IMO problem.

2. **Original Proof:**
   The solver's original, unmodified proof of the problem.

3. **Rewritten Proof (Proposed Solution):**
   A structured rewrite of the original proof, decomposed into
   propositions and lemmas. This rewrite is produced by an automated
   rewriter and may itself introduce mistakes, omissions, or distortions
   that were **not** present in the original proof.

4. **Potential Errors:**
   A list of potential errors that were identified in specific components
   (lemmas, propositions, or the theorem) of the rewritten proof. These
   potential errors may or may not be genuine errors in the underlying
   solution — in particular, an "error" may be an artifact of the
   rewriting process (e.g., the rewriter dropped a key step, misstated a
   claim, or restructured the argument in a way that obscures correct
   reasoning that **is** present in the original proof).

### Evaluation Process

You must follow this process:

1. **Error Validation:**
   For each potential error, carefully determine whether it is a
   **genuine error in the underlying solution** or a **false alarm**.
   Examine the error in the context of the full rewritten proof, **and
   cross-check against the original proof** to see whether the
   reasoning the rewritten proof is missing or misstating actually
   appears (correctly) in the original. If so, treat the error as a
   rewriting artifact rather than a genuine error in the solution.

2. **Score Determination:**
   Assign the final score to the **original proof**. Use only the
   errors you determined to be genuine errors *in the original proof*
   (after cross-checking against it) when applying the General Rubric.
   Do **not** penalize the original proof for mistakes that were
   introduced by the rewriter, and do **not** grade the quality of the
   rewrite itself.

### Output Requirements

You must provide your final score in the format:

<points>N out of 7</points>

Ensure the `<points>` block is used **only once**, as your answer will be
parsed based on the first `<points></points>` block that appears in your
entire response.

**PROBLEM STATEMENT**
{problem}

**ORIGINAL PROOF**
{original_proof}

**REWRITTEN PROOF (PROPOSED SOLUTION)**
{proof}

**POTENTIAL ERRORS (from verification of the rewritten proof)**
{errors}

For each potential error, explain whether it is a genuine error **in
the original proof** or a false alarm (including the case where the
rewriting process introduced or fabricated the issue while the original
proof handles it correctly). Then provide your final score **for the
original proof**.

[Select one of the following options]

<points>7 out of 7</points>
<points>6 out of 7</points>
<points>1 out of 7</points>
<points>0 out of 7</points>
"""

HARD2VERIFY_STEP_META_VERIFY_PROMPT = """You are a strict, reliable mathematical proof grader.
Your task is to evaluate the ORIGINAL solution to a math problem, step by step.

The structured rewrite and the list of potential errors are diagnostic aids only. They are not what you are grading. The rewrite may introduce omissions, distortions, false claims, or artificial dependencies that were not present in the original solution.

You must decide which original solution steps are mathematically correct or incorrect, and identify the first incorrect original step.

Input data:

1. Math Problem:
   The original problem statement.

2. Original Solution Steps:
   The solver's original solution split into indexed steps, written as:
   <step>[0] ...</step>
   <step>[1] ...</step>
   and so on.

3. Rewritten Proof:
   A structured rewrite of the original solution, decomposed into theorem / proposition / lemma / claim / fact blocks. This rewrite was produced automatically and may contain artifacts that are not present in the original solution.

4. Potential Errors:
   A list of potential errors flagged by automated verification of the rewritten proof. These may or may not be genuine errors in the original solution.

Evaluation process:

1. Validate each potential error.
   For each flagged issue, decide whether it is a genuine mathematical error in the ORIGINAL solution or a false alarm caused by the rewrite. Cross-check against the original solution steps. If the original solution contains the missing reasoning, a stronger equivalent argument, or a correct alternative route, do not count the flag as an original-solution error.

2. Inspect the original steps as needed.
   The potential errors are useful leads, but your final output must be based on the original solution steps. Check any original steps needed to identify the first genuine incorrect step. If you find that the first incorrect step was not covered by any potential error, report it in <additional_errors>.

3. Grade the original steps, not the rewrite.
   For each original step, determine whether that step is correct or incorrect.
   - A correct step is one where all mathematical content is correct and logically consistent with the problem and all previous correct steps.
   - An incorrect step is one that contains a mathematical error, an unjustified load-bearing claim, a logical inconsistency, a misapplied theorem, a false computation, or reasoning that depends on an earlier incorrect step.
   - If a step merely repeats or depends essentially on an earlier incorrect step, mark it incorrect as well.
   - Do not mark a step incorrect for harmless terseness, routine omitted algebra, stylistic issues, or rewrite artifacts.

4. Identify the first incorrect step.
   The first incorrect step is the smallest original step index whose content is mathematically incorrect or whose reasoning first depends on an error.
   If every original step is correct, output -1.

Important calibration rules:

- The original solution steps are authoritative for the final verdict.
- The rewritten proof and potential errors are evidence, not ground truth.
- Do not penalize the original solution for mistakes introduced only by the rewriter.
- Do not excuse a genuine gap in the original solution merely because the rewrite attempted to repair it.
- Focus on mathematical substance: incorrect proof steps, false claims, unjustified key lemmas, invalid dependencies, incorrect computations, and misapplied results.
- Ignore typos, formatting, wording preferences, and non-load-bearing presentation issues.
- Be strict but not pedantic: standard facts, routine algebra, and obvious intermediate manipulations need not be fully spelled out when they are mathematically valid and unambiguous.

Output requirements:

Return your final answer using exactly the XML-style structure below.
Do not use Markdown, bullet lists, code fences, or any text after the closing </calibration> tag.
Inside descriptions you may write LaTeX freely.

<calibration>
  <flag_audit>
    <flag>
      <source>Brief identifier of the potential error being audited, such as the rewritten block name or a short quote.</source>
      <status>genuine</status>
      <original_step>the original step index most directly affected, or -1 if no single step applies</original_step>
      <explanation>Brief explanation of why this is a genuine error in the original solution.</explanation>
    </flag>
    <flag>
      <source>...</source>
      <status>false_alarm</status>
      <original_step>-1</original_step>
      <explanation>Brief explanation of why this is only a rewrite artifact or otherwise not an error in the original solution.</explanation>
    </flag>
  </flag_audit>
  <additional_errors>
    <error>
      <original_step>step index</original_step>
      <description>Brief description of any genuine original-solution error not already covered by a potential flag.</description>
    </error>
  </additional_errors>
  <step_verdicts>yes,no,yes,...</step_verdicts>
  <first_incorrect_step>N</first_incorrect_step>
</calibration>

Rules for the parsed fields:

- <step_verdicts> must contain exactly {num_steps} comma-separated entries.
- Each entry must be exactly yes or no.
- yes means the corresponding original step is correct.
- no means the corresponding original step is incorrect.
- <first_incorrect_step> must be the first index whose step verdict is no.
- If every step verdict is yes, <first_incorrect_step> must be -1.
- If there are no potential flags to audit, output an empty <flag_audit> block.
- If there are no additional errors, output an empty <additional_errors> block.

MATH PROBLEM:
{problem}

ORIGINAL SOLUTION STEPS:
{steps}

REWRITTEN PROOF:
{rewritten_proof}

POTENTIAL ERRORS FROM REWRITTEN-PROOF VERIFICATION:
{errors}"""


ARXIV_REFEREE_PROMPT = """You are a mathematical referee reviewing a paper submitted to a peer-reviewed mathematics journal. You are given the paper in two forms:

1. The rendered PDF (attached as a file). Use this to read the paper as a human reader would, with all theorem/lemma/proposition numbers rendered.
2. The raw LaTeX source (included below). Use this to inspect precise notation, equations, and label names if needed.

Your task is to identify any mathematical errors in the paper that would require revision before publication. Focus on errors of mathematical substance: incorrect proofs, unjustified steps, false claims, gaps in reasoning, miscomputed quantities, misapplied theorems, and similar issues. Do not report typos, stylistic concerns, formatting issues, or notational preferences.

CRITICAL — how to identify locations:

For each error you find, identify the location using the rendered label EXACTLY as it appears in the PDF. For example: "Theorem 19", "Lemma 2.3", "Proposition 5.1", "Corollary 3.5", "Theorem B".

Do NOT use raw LaTeX `\\ref{{...}}` or `\\label{{...}}` references — always quote the rendered numerical or letter label as a human reader would see it on the page of the PDF.

If the error is in an unlabelled passage (no Theorem/Lemma/Proposition number applies), name the surrounding section heading or use a short descriptive locator that a reader could find in the PDF (for example "Section 3.2, paragraph after Definition 2.1").

OUTPUT FORMAT:

After your analysis, output your final answer using the following XML-style format. Use one `<error>` block per error. Use the field tags exactly as shown. You may write LaTeX math (with raw backslashes) freely inside the `<description>` field; do not escape anything. Do not output anything after the closing `</errors>` tag.

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

If you find no mathematical errors, output an empty errors block:

<errors>
</errors>

LATEX SOURCE:
{paper_tex}
"""


# =============================================================================
# Decomposed-rewriter prompts for arxiv papers.
# These mirror the IMO REWRITE / REGENERATE / FAITHFULNESS / COMPONENT_VERIFY /
# META_VERIFY prompts but adapted for whole-paper input (multiple top-level
# theorems, PDF + raw tex inputs, output errors keyed by PDF labels).
# =============================================================================


ARXIV_REWRITE_PROMPT = """Rewrite the mathematical paper below into a structured formal proof outline.

You are given:
1. The rendered PDF of the paper (attached as a file). Use it to read the paper as a human would and to identify the rendered numerical or letter labels of every theorem/lemma/proposition/corollary/claim.
2. The raw LaTeX source of the paper (included below). Use it to inspect precise notation, equations, and the exact wording of each statement and proof.

Structure:
- The top level of the rewrite is a list of **theorems**. Use one `<THEOREM_STATEMENT id="N">` block per top-level result the paper proves. Top-level results are the paper's flagship theorem(s) plus any independent secondary theorems / propositions / corollaries that the paper states as headline results (i.e. results that are not themselves used merely as intermediate steps for another result).
- Below the theorems, use at most 2 further layers in the proof tree:
  1. Propositions (shared pool, numbered globally across all theorems)
  2. Lemmas (numbered within each proposition)
- Do not introduce any deeper hierarchy. If a deeper tree seems natural, flatten it into a sequential list of lemmas inside the relevant proposition.
- Each `<THEOREM_PROOF id="N">` may cite any of the propositions; a `<PROPOSITION_PROOF id="K">` may cite its own lemmas and the statements of earlier propositions. A theorem proof must NOT cite another theorem unless that theorem appears earlier in the rewrite (i.e. has a smaller id) and the citation reflects an actual dependency in the paper.
- No trivial decompositions: do not decompose a theorem into a single proposition that restates the theorem, nor a proposition into a single lemma that restates the proposition.
- Every rewritten assertion must be unambiguous: definitions and statements must be precise, and each variable must have a single, clear meaning within an assertion.

Numbering and order:
- Number theorems sequentially: `<THEOREM_STATEMENT id="1">`, `<THEOREM_STATEMENT id="2">`, ...
  - Order theorems in the order they appear in the paper.
- Number propositions sequentially across the whole paper: 1, 2, 3, ... (a single shared pool, NOT per-theorem).
- Number lemmas within each proposition: 1.1, 1.2, ..., 2.1, 2.2, ...
- The proof must read top-to-bottom as a forward sequence: if component j uses component i at the same level, then j > i. Reorder to avoid forward references.

Faithfulness to the original paper:
- Preserve the paper's mathematical content, notation, logical flow, ordering, and wording as much as possible.
- For low-level arguments inside leaf components, avoid changing the original wording.
- Only make the minimal edits needed to fit the structured format.
- Do not introduce alternative arguments, and do not repair, optimize, strengthen, or silently fix the paper.
- Do not add justifications absent from the original paper, omit relevant proof details, or introduce statements stronger than what the original paper establishes.

Assumptions, conditions, and definitions:
- Clearly state the assumptions, conditions, and definitions for every theorem, proposition, and lemma.
- Each component may inherit the setting of its enclosing parent, but if tracing back through multiple earlier statements would be needed, restate the relevant assumptions explicitly.
- If a component modifies its parent's setting, explicitly state the full updated assumptions and note which assumptions were added, removed, or changed relative to the parent.

Output format:
Wrap every section in XML-style delimiter tags as shown in the template below.
- Use EXACTLY the tag names shown: THEOREM_STATEMENT, PROPOSITION_STATEMENT, LEMMA_STATEMENT, LEMMA_PROOF, PROPOSITION_PROOF, THEOREM_PROOF.
- Every tag MUST have an id attribute matching the numbering scheme above. (Including THEOREM_STATEMENT and THEOREM_PROOF — unlike other rewrite formats you may have seen.)
- Do NOT nest tags inside each other. All tags are at the top level.
- Do NOT include any text outside of tags.
- Include all assumptions, conditions, and definitions INSIDE the statement tag they belong to.
- Use LaTeX notation for all mathematical notations.

Template:

<THEOREM_STATEMENT id="1">
Assumptions / Conditions / Definitions.
- ...
Statement :
...
</THEOREM_STATEMENT>

<THEOREM_STATEMENT id="2">
Assumptions / Conditions / Definitions.
- ...
Statement :
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

<LEMMA_PROOF id="1.1">
[proof.]
</LEMMA_PROOF>

<PROPOSITION_PROOF id="1">
[explain how Lemmas 1.1, 1.2 imply Proposition 1.]
</PROPOSITION_PROOF>

<PROPOSITION_STATEMENT id="2">
...
</PROPOSITION_STATEMENT>

...

<THEOREM_PROOF id="1">
[explain how Propositions 1, 3, ... imply Theorem 1.]
</THEOREM_PROOF>

<THEOREM_PROOF id="2">
[explain how Propositions 2, 4, ... imply Theorem 2.]
</THEOREM_PROOF>


Now rewrite the following paper in this format. The PDF is attached as a separate file; the raw LaTeX is below.

LATEX SOURCE:
{paper_tex}
"""


ARXIV_REGENERATE_REWRITE_PROMPT = """You previously produced a structured rewrite of a mathematical paper, but a faithfulness checker identified discrepancies between your rewrite and the original paper.

Your task is to produce a NEW rewritten paper that fixes the identified issues while still following the structured format.

You will receive:
1. The rendered PDF of the original paper (attached as a file).
2. The raw LaTeX source of the paper (included below — ground truth — your rewrite must faithfully represent this).
3. Your previous rewritten paper, which contained faithfulness errors.
4. The specific discrepancies flagged by the checker.

Requirements for the new rewrite:
- Fix every issue listed in the Identified Errors. For each issue, make sure the new rewrite no longer deviates from the original paper in that way.
- Do NOT introduce new discrepancies: do not strengthen/weaken claims, omit steps, add arguments, drift in notation, or misrepresent the logical structure relative to the original paper.
- Preserve the original paper's content, notation, logical flow, ordering, and wording as much as possible.
- Follow the SAME structured output format (XML-style tags, numbering, multiple `<THEOREM_STATEMENT id="N">` blocks at the top, etc.) as the rewriting instructions below.

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


ARXIV_COMPONENT_FAITHFULNESS_PROMPT = """You are an expert mathematician reviewing whether a component of a rewritten mathematical paper faithfully represents the corresponding part of the original paper.

You are given:
1. **Original Paper (LaTeX)**: The full LaTeX source of the original paper.
2. **Contexts**: Statements from the rewritten paper that the Assertion inherits definitions or assumptions from (e.g., the enclosing theorem statement, the enclosing proposition statement). These are provided so you can understand the scope of the Assertion.
3. **Established Results**: Statements from the rewritten paper that have already been checked and can be assumed to be faithfully rewritten. You may use them as reference points.
4. **Assertion**: The specific rewritten statement whose faithfulness you must verify.
5. **Proposed Proof**: The rewritten proof of the Assertion (may be empty for a top-level theorem statement).

Your task is to determine whether the Assertion and its Proposed Proof **faithfully represent** the corresponding part of the Original Paper.

Check for:
1. **Strengthened or weakened claims**: Does the Assertion claim more or less than the original paper establishes at the corresponding point?
2. **Omitted content**: Does the Proposed Proof drop a non-trivial argument that appears in the original paper?
3. **Added content**: Does the Proposed Proof introduce new arguments, repairs, or proof ideas not present in the original paper?
4. **Notation drift**: Are variables, functions, or definitions used differently than in the original paper?
5. **Misinterpretation**: Does the Assertion or Proposed Proof misunderstand the original's reasoning or logical structure?
6. **Scope errors**: Are assumptions incorrectly inherited, dropped, or added compared to the original?

Instructions:
- Do NOT judge whether the original paper is mathematically correct. Your sole task is faithfulness.
- Only flag changes that alter mathematical meaning. Cosmetic rephrasing is fine.
- Use the Contexts to understand what the Assertion is allowed to assume.
- Use the Established Results as anchors: if a prior component was faithful, you can compare the current component's references to it.

At the very end of your response, output your verdict as a JSON block. Do NOT write anything after the JSON block.

If FAITHFUL:
```json
{{"verdict": "FAITHFUL", "error_description": null}}
```

If UNFAITHFUL:
```json
{{"verdict": "UNFAITHFUL", "error_description": "Identify the specific discrepancy: what the rewrite says vs. what the original paper says."}}
```

**ORIGINAL PAPER (LATEX)**
{original_paper}

**CONTEXTS**
{contexts}

**ESTABLISHED RESULTS**
{established_results}

**ASSERTION**
{assertion}

**PROPOSED PROOF**
{proof}
"""


ARXIV_META_VERIFY_PROMPT = """You are an expert mathematical referee. Your task is to produce the FINAL list of mathematical errors in a peer-reviewed mathematics paper.

You are given:
1. The rendered PDF of the original paper (attached as a file). This is your authoritative source for the labels you must use in the final answer (e.g. "Theorem 19", "Lemma 4.3", "Proposition 5.1", "Theorem B").
2. The raw LaTeX source of the paper (included below). Use this for precise notation, equations, and exact wording.
3. A structured rewrite of the paper, decomposed into theorems / propositions / lemmas. The rewrite was produced by an automated rewriter and may itself introduce mistakes, omissions, or distortions that were **not** present in the original paper.
4. A list of potential errors that an automated component-verifier flagged in specific components (lemmas, propositions, or theorems) of the rewritten paper. These potential errors may or may not be genuine errors in the underlying paper — in particular, an "error" may be an artifact of the rewriting process (e.g., the rewriter dropped a key step, misstated a claim, or restructured the argument in a way that obscures correct reasoning that **is** present in the original paper).

Evaluation process:

1. **Error validation.** For each potential error, carefully determine whether it is a genuine error in the underlying paper or a false alarm. Examine the error in the context of the full rewritten paper, AND cross-check against the original paper (PDF + LaTeX) to see whether the reasoning the rewritten paper is missing or misstating actually appears (correctly) in the original. If so, treat the error as a rewriting artifact rather than a genuine error in the paper.

2. **Search for additional errors.** You may also report errors in the original paper that the component verifier did NOT flag — including errors in unlabelled prose passages, between numbered results, or that span multiple components. Do not feel constrained to only the listed potential errors.

3. **Report only mathematical errors.** Focus on errors of mathematical substance (incorrect proofs, unjustified steps, false claims, gaps in reasoning, miscomputed quantities, misapplied theorems). Do not report typos, stylistic concerns, formatting issues, or notational preferences.

CRITICAL — labelling of locations in the final answer:

For every error you report, the `<location>` field MUST use the rendered label exactly as it appears IN THE PDF. Examples: "Theorem 19", "Lemma 4.3", "Proposition 5.1", "Corollary 3.5", "Theorem B".

Do NOT use:
- Internal rewrite-tree labels like "Proposition 7" or "Lemma 1.2" (those are the rewriter's invented numbering and do not match the paper's numbering).
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


# =============================================================================
# PDF-only variants of the arxiv prompts.
# Identical to the prompts above except the model receives ONLY the rendered
# PDF (attached as a file) — no raw LaTeX source. Used by the PDF-only input
# mode so the benchmark can ship/download PDFs without redistributing source.
# NOTE: ARXIV_REFEREE_PROMPT_PDF_ONLY and ARXIV_REWRITE_PROMPT_PDF_ONLY are
# NOT passed through str.format(), so they use single braces.
# =============================================================================


ARXIV_REFEREE_PROMPT_PDF_ONLY = """You are a mathematical referee reviewing a paper submitted to a peer-reviewed mathematics journal. You are given the paper as a rendered PDF (attached as a file). Read the paper as a human reader would, with all theorem/lemma/proposition numbers rendered, and inspect the notation and equations directly in the PDF.

Your task is to identify any mathematical errors in the paper that would require revision before publication. Focus on errors of mathematical substance: incorrect proofs, unjustified steps, false claims, gaps in reasoning, miscomputed quantities, misapplied theorems, and similar issues. Do not report typos, stylistic concerns, formatting issues, or notational preferences.

CRITICAL — how to identify locations:

For each error you find, identify the location using the rendered label EXACTLY as it appears in the PDF. For example: "Theorem 19", "Lemma 2.3", "Proposition 5.1", "Corollary 3.5", "Theorem B".

If the error is in an unlabelled passage (no Theorem/Lemma/Proposition number applies), name the surrounding section heading or use a short descriptive locator that a reader could find in the PDF (for example "Section 3.2, paragraph after Definition 2.1").

OUTPUT FORMAT:

After your analysis, output your final answer using the following XML-style format. Use one `<error>` block per error. Use the field tags exactly as shown. You may write LaTeX math (with raw backslashes) freely inside the `<description>` field; do not escape anything. Do not output anything after the closing `</errors>` tag.

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

If you find no mathematical errors, output an empty errors block:

<errors>
</errors>
"""


ARXIV_REWRITE_PROMPT_PDF_ONLY = """Rewrite the mathematical paper below into a structured formal proof outline.

You are given the rendered PDF of the paper (attached as a file). Read the paper as a human would, inspect the precise notation, equations, and exact wording of each statement and proof directly in the PDF, and identify the rendered numerical or letter labels of every theorem/lemma/proposition/corollary/claim.

Structure:
- The top level of the rewrite is a list of **theorems**. Use one `<THEOREM_STATEMENT id="N">` block per top-level result the paper proves. Top-level results are the paper's flagship theorem(s) plus any independent secondary theorems / propositions / corollaries that the paper states as headline results (i.e. results that are not themselves used merely as intermediate steps for another result).
- Below the theorems, use at most 2 further layers in the proof tree:
  1. Propositions (shared pool, numbered globally across all theorems)
  2. Lemmas (numbered within each proposition)
- Do not introduce any deeper hierarchy. If a deeper tree seems natural, flatten it into a sequential list of lemmas inside the relevant proposition.
- Each `<THEOREM_PROOF id="N">` may cite any of the propositions; a `<PROPOSITION_PROOF id="K">` may cite its own lemmas and the statements of earlier propositions. A theorem proof must NOT cite another theorem unless that theorem appears earlier in the rewrite (i.e. has a smaller id) and the citation reflects an actual dependency in the paper.
- No trivial decompositions: do not decompose a theorem into a single proposition that restates the theorem, nor a proposition into a single lemma that restates the proposition.
- Every rewritten assertion must be unambiguous: definitions and statements must be precise, and each variable must have a single, clear meaning within an assertion.

Numbering and order:
- Number theorems sequentially: `<THEOREM_STATEMENT id="1">`, `<THEOREM_STATEMENT id="2">`, ...
  - Order theorems in the order they appear in the paper.
- Number propositions sequentially across the whole paper: 1, 2, 3, ... (a single shared pool, NOT per-theorem).
- Number lemmas within each proposition: 1.1, 1.2, ..., 2.1, 2.2, ...
- The proof must read top-to-bottom as a forward sequence: if component j uses component i at the same level, then j > i. Reorder to avoid forward references.

Faithfulness to the original paper:
- Preserve the paper's mathematical content, notation, logical flow, ordering, and wording as much as possible.
- For low-level arguments inside leaf components, avoid changing the original wording.
- Only make the minimal edits needed to fit the structured format.
- Do not introduce alternative arguments, and do not repair, optimize, strengthen, or silently fix the paper.
- Do not add justifications absent from the original paper, omit relevant proof details, or introduce statements stronger than what the original paper establishes.

Assumptions, conditions, and definitions:
- Clearly state the assumptions, conditions, and definitions for every theorem, proposition, and lemma.
- Each component may inherit the setting of its enclosing parent, but if tracing back through multiple earlier statements would be needed, restate the relevant assumptions explicitly.
- If a component modifies its parent's setting, explicitly state the full updated assumptions and note which assumptions were added, removed, or changed relative to the parent.

Output format:
Wrap every section in XML-style delimiter tags as shown in the template below.
- Use EXACTLY the tag names shown: THEOREM_STATEMENT, PROPOSITION_STATEMENT, LEMMA_STATEMENT, LEMMA_PROOF, PROPOSITION_PROOF, THEOREM_PROOF.
- Every tag MUST have an id attribute matching the numbering scheme above. (Including THEOREM_STATEMENT and THEOREM_PROOF — unlike other rewrite formats you may have seen.)
- Do NOT nest tags inside each other. All tags are at the top level.
- Do NOT include any text outside of tags.
- Include all assumptions, conditions, and definitions INSIDE the statement tag they belong to.
- Use LaTeX notation for all mathematical notations.

Template:

<THEOREM_STATEMENT id="1">
Assumptions / Conditions / Definitions.
- ...
Statement :
...
</THEOREM_STATEMENT>

<THEOREM_STATEMENT id="2">
Assumptions / Conditions / Definitions.
- ...
Statement :
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

<LEMMA_PROOF id="1.1">
[proof.]
</LEMMA_PROOF>

<PROPOSITION_PROOF id="1">
[explain how Lemmas 1.1, 1.2 imply Proposition 1.]
</PROPOSITION_PROOF>

<PROPOSITION_STATEMENT id="2">
...
</PROPOSITION_STATEMENT>

...

<THEOREM_PROOF id="1">
[explain how Propositions 1, 3, ... imply Theorem 1.]
</THEOREM_PROOF>

<THEOREM_PROOF id="2">
[explain how Propositions 2, 4, ... imply Theorem 2.]
</THEOREM_PROOF>


Now rewrite the following paper in this format. The paper is provided as the attached PDF.
"""


ARXIV_REGENERATE_REWRITE_PROMPT_PDF_ONLY = """You previously produced a structured rewrite of a mathematical paper, but a faithfulness checker identified discrepancies between your rewrite and the original paper.

Your task is to produce a NEW rewritten paper that fixes the identified issues while still following the structured format.

You will receive:
1. The rendered PDF of the original paper (attached as a file — ground truth — your rewrite must faithfully represent this).
2. Your previous rewritten paper, which contained faithfulness errors.
3. The specific discrepancies flagged by the checker.

Requirements for the new rewrite:
- Fix every issue listed in the Identified Errors. For each issue, make sure the new rewrite no longer deviates from the original paper in that way.
- Do NOT introduce new discrepancies: do not strengthen/weaken claims, omit steps, add arguments, drift in notation, or misrepresent the logical structure relative to the original paper.
- Preserve the original paper's content, notation, logical flow, ordering, and wording as much as possible.
- Follow the SAME structured output format (XML-style tags, numbering, multiple `<THEOREM_STATEMENT id="N">` blocks at the top, etc.) as the rewriting instructions below.

--- Original rewriting instructions (follow these for the output format) ---
{rewrite_instructions}
--- End of rewriting instructions ---

PREVIOUS REWRITTEN PAPER (contains errors):
{previous_rewrite}

IDENTIFIED ERRORS:
{errors}

Now produce the corrected rewritten paper. Output ONLY the rewritten paper using the XML-tag format — no commentary before or after.
"""


ARXIV_COMPONENT_FAITHFULNESS_PROMPT_PDF_ONLY = """You are an expert mathematician reviewing whether a component of a rewritten mathematical paper faithfully represents the corresponding part of the original paper.

You are given:
1. **Original Paper (PDF)**: The rendered PDF of the original paper, attached as a file. This is the ground truth.
2. **Contexts**: Statements from the rewritten paper that the Assertion inherits definitions or assumptions from (e.g., the enclosing theorem statement, the enclosing proposition statement). These are provided so you can understand the scope of the Assertion.
3. **Established Results**: Statements from the rewritten paper that have already been checked and can be assumed to be faithfully rewritten. You may use them as reference points.
4. **Assertion**: The specific rewritten statement whose faithfulness you must verify.
5. **Proposed Proof**: The rewritten proof of the Assertion (may be empty for a top-level theorem statement).

Your task is to determine whether the Assertion and its Proposed Proof **faithfully represent** the corresponding part of the Original Paper.

Check for:
1. **Strengthened or weakened claims**: Does the Assertion claim more or less than the original paper establishes at the corresponding point?
2. **Omitted content**: Does the Proposed Proof drop a non-trivial argument that appears in the original paper?
3. **Added content**: Does the Proposed Proof introduce new arguments, repairs, or proof ideas not present in the original paper?
4. **Notation drift**: Are variables, functions, or definitions used differently than in the original paper?
5. **Misinterpretation**: Does the Assertion or Proposed Proof misunderstand the original's reasoning or logical structure?
6. **Scope errors**: Are assumptions incorrectly inherited, dropped, or added compared to the original?

Instructions:
- Do NOT judge whether the original paper is mathematically correct. Your sole task is faithfulness.
- Only flag changes that alter mathematical meaning. Cosmetic rephrasing is fine.
- Use the Contexts to understand what the Assertion is allowed to assume.
- Use the Established Results as anchors: if a prior component was faithful, you can compare the current component's references to it.

At the very end of your response, output your verdict as a JSON block. Do NOT write anything after the JSON block.

If FAITHFUL:
```json
{{"verdict": "FAITHFUL", "error_description": null}}
```

If UNFAITHFUL:
```json
{{"verdict": "UNFAITHFUL", "error_description": "Identify the specific discrepancy: what the rewrite says vs. what the original paper says."}}
```

**ORIGINAL PAPER**
The original paper is the PDF attached to this message.

**CONTEXTS**
{contexts}

**ESTABLISHED RESULTS**
{established_results}

**ASSERTION**
{assertion}

**PROPOSED PROOF**
{proof}
"""


ARXIV_META_VERIFY_PROMPT_PDF_ONLY = """You are an expert mathematical referee. Your task is to produce the FINAL list of mathematical errors in a peer-reviewed mathematics paper.

You are given:
1. The rendered PDF of the original paper (attached as a file). This is your authoritative source for the paper's content — precise notation, equations, exact wording — and for the labels you must use in the final answer (e.g. "Theorem 19", "Lemma 4.3", "Proposition 5.1", "Theorem B").
2. A structured rewrite of the paper, decomposed into theorems / propositions / lemmas. The rewrite was produced by an automated rewriter and may itself introduce mistakes, omissions, or distortions that were **not** present in the original paper.
3. A list of potential errors that an automated component-verifier flagged in specific components (lemmas, propositions, or theorems) of the rewritten paper. These potential errors may or may not be genuine errors in the underlying paper — in particular, an "error" may be an artifact of the rewriting process (e.g., the rewriter dropped a key step, misstated a claim, or restructured the argument in a way that obscures correct reasoning that **is** present in the original paper).

Evaluation process:

1. **Error validation.** For each potential error, carefully determine whether it is a genuine error in the underlying paper or a false alarm. Examine the error in the context of the full rewritten paper, AND cross-check against the original paper (the attached PDF) to see whether the reasoning the rewritten paper is missing or misstating actually appears (correctly) in the original. If so, treat the error as a rewriting artifact rather than a genuine error in the paper.

2. **Search for additional errors.** You may also report errors in the original paper that the component verifier did NOT flag — including errors in unlabelled prose passages, between numbered results, or that span multiple components. Do not feel constrained to only the listed potential errors.

3. **Report only mathematical errors.** Focus on errors of mathematical substance (incorrect proofs, unjustified steps, false claims, gaps in reasoning, miscomputed quantities, misapplied theorems). Do not report typos, stylistic concerns, formatting issues, or notational preferences.

CRITICAL — labelling of locations in the final answer:

For every error you report, the `<location>` field MUST use the rendered label exactly as it appears IN THE PDF. Examples: "Theorem 19", "Lemma 4.3", "Proposition 5.1", "Corollary 3.5", "Theorem B".

Do NOT use:
- Internal rewrite-tree labels like "Proposition 7" or "Lemma 1.2" (those are the rewriter's invented numbering and do not match the paper's numbering).
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

REWRITTEN PAPER (PROPOSED STRUCTURED FORM):
{rewritten_paper}

POTENTIAL ERRORS (from automated verification of the rewritten paper):
{errors}
"""
