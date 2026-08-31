"""Prompt templates for SkillZip's model-backed stages (paper prompts/{extract,
patch,audit}.txt). Kept as importable strings; the CLI also mirrors them under
skillzip/prompts/ as plain text for auditability."""

EXTRACT_PROMPT = """You convert one agent SKILL document into a TYPED CONTRACT.
Do NOT compress, summarize, merge, or delete anything. Only classify existing
content into typed units and cite the source block id(s) each unit came from.

Return STRICT JSON: {"units":[{...}, ...]}. Each unit has:
  "id": short unique string
  "type": one of interface|workflow|tool|rule|output|evidence
  "scope": array like ["root"] or ["root","output"] or ["root","when-<guard>"]
  "guard": condition text if the unit only applies conditionally, else ""
  "modality": for rules -> must|must_not|should ; else "info"
  "content": the normalized requirement text (verbatim meaning, no invention)
  "provenance": array of source block ids [b...] that support this unit
  # type extras (include when relevant):
  "role": for interface -> name|purpose|trigger|exclusion
  "tool": tool name (tool units); "args": [required argument names]
  "fields": [required output field names]; "validation": completion condition
Rules:
- Every unit MUST cite at least one real block id from the list.
- Write "content" in the SAME language as the source document (Chinese source
  stays Chinese, English stays English); NEVER translate.
- A prohibition ("never/do not") is modality=must_not.
- Two sentences about the same tool with different args are DIFFERENT units.
- If a span is ambiguous, omit it (it will be preserved verbatim).
Reply with ONLY the JSON.

## SOURCE BLOCKS
{{BLOCKS}}
"""

RELATION_PROMPT = """You are a frozen relation checker for two typed skill units.
Decide their logical relation. Reply with ONLY one word:
  equivalence      (same requirement, paraphrase)
  left_implication (A implies B: A is stronger/more specific)
  right_implication(B implies A: B is stronger/more specific)
  conflict         (they require incompatible behavior)
  unrelated        (different requirements)

A: [{a_mod}] (guard: {a_guard}) {a_text}
B: [{b_mod}] (guard: {b_guard}) {b_text}
"""

PATCH_PROMPT = """You maintain a compressed agent skill during self-evolution.
A new PATCH unit arrived. Compared with the retrieved COMPATIBLE units from the
current compact contract, choose exactly ONE operation for the patch:
  ABSORB   - patch restates an existing requirement, adds no new content
  REFINE   - patch adds a guard, tool argument, validation, or exception to an
             existing unit (name it)
  EXTEND   - patch introduces a genuinely new requirement
  REFACTOR - patch makes a shared rule/workflow newly worthwhile
Return STRICT JSON: {"op":"ABSORB|REFINE|EXTEND|REFACTOR","target":"<unit id or empty>","reason":"..."}.
Do not accept an operation because it improves any task score.

## PATCH UNIT
[{p_mod}] (guard: {p_guard}) {p_text}

## COMPATIBLE UNITS
{{CANDIDATES}}
Reply with ONLY the JSON.
"""

AUDIT_PROMPT = """Parse this compressed SKILL into a typed contract for auditing.
Return STRICT JSON {"units":[...]} using the same schema as extraction. Do not
compress. Reply with ONLY the JSON.

## COMPRESSED SKILL
{{SKILL}}
"""
