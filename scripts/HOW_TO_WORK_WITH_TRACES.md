# How to analyze traces and improve SKILL.md

## Goal

Improve SKILL.md by identifying missing or incorrect guidance that causes
failure patterns across your traces. Produce a **targeted diff**, not a
rewrite.

## Step 1 — Inventory

1. List all traces in your `.traces/` directory. Read `.traces/index.json`
   if it exists, otherwise `ls .traces/`.
2. For each trace, read its `result.json` and extract:
   - `status` (pass/fail/timeout/corrupted)
   - `error` or `error_reason` (if present)
   - `returncode` (if present)
   - `test_passed` / `test_total` (if present)
   - Any other metrics available (tokens_in, tokens_out, elapsed_sec, etc.)
3. Build a count of how many traces fall into each status category.

## Step 2 — Categorize failures

Group all failing traces by root cause. Look for these patterns in the
error fields and test outcomes:

| Category | What to look for |
|---|---|
| `extraction_failed` | LLM didn't produce a valid Python code block |
| `transport_failed` | LLM returned empty response or no tokens |
| `code_extraction_failed` | Model produced code but format/structure was wrong |
| `exit_code_N` | Solution script crashed with non-zero exit |
| `assertion_mismatch` | Script ran but test assertions failed |
| `timeout` | Script ran too long (exceeded timeout budget) |
| `invalid_candidate` | Post-processing (rewrite guard) rejected the output |
| `leak_detected` | Output contains leaked artifacts |
| `reflect_failed` | Reflection step itself failed |

Count how many traces fall into each category. Note the **top 2-3 most
frequent categories** for deep-dive.

**Rule**: Only analyze failure categories with more than 1 occurrence.
Skip single-occurrence anomalies unless they reveal a clear pattern.

## Step 3 — Deep-dive representative traces

For each major failure category (keep it to the top 2-3 most common):

1. Find one trace where this failure occurred.
2. Read `result.json` → note the exact error message.
3. Read `solve.py` → what did the solution attempt? What approach did it take?
4. Read `response.md` → what did the LLM actually output? Was it a code
   block? Was there extra text?
5. Compare: what did SKILL.md say vs. what the LLM did vs. what happened.
6. Identify the **gap**: which section of SKILL.md was missing, wrong, or
   ambiguous?

For **failing traces**: focus on the point where the solution diverged
from what SKILL.md instructed.

For **partial-pass traces** (if found): focus on why some tests pass and
others fail. This often reveals a partially-covered but incomplete rule.

## Step 4 — Cross-reference SKILL.md

Open `SKILL.md` and evaluate each major section against the failure
patterns identified in Step 2:

- **Execution contract** — Does it tell the model exactly how to run its
  code, where to write output, and what to expect from the environment?
- **File structure** — Does it specify which files to create, where, and
  in what format?
- **Error handling / common pitfalls** — Does it cover the failure patterns
  you observed?
- **Output format** — Does it define the expected output format clearly?
- **Code examples** — Do examples cover the cases that failed?
- **Tool / library usage** — Does it specify which packages to use or avoid?

For each gap, classify it:

| Classification | Meaning |
|---|---|
| **Covers** | Section already addresses this pattern (no change needed) |
| **Wrong** | Section exists but gives incorrect or misleading guidance |
| **Missing** | Section exists but doesn't cover this pattern |
| **New** | No existing section covers this — needs a new section |

Only propose changes for classifications of **Wrong**, **Missing**, or
**New**. Skip sections marked as **Covers**.

## Step 5 — Propose the diff

For each change, produce a structured entry with:

1. **Section name** — the exact heading being modified (or "New Section"
   if the section needs to be added)
2. **Classification** — Wrong / Missing / New
3. **Problem** — what's wrong or missing, referencing the failure category
4. **Failure pattern** — the category from Step 2 and how many traces it
   affects
5. **Fix** — the specific guidance to add or modify
6. **Reason** — why this change addresses the failure pattern

### Output format

After analysis, produce a structured summary in this format:

```
# SKILL.md improvement for {skill}

## Summary
- {N} traces analyzed
- {M} failure categories found
- Top pattern: {category} — {count} occurrences

## Changes

### [Section: {section_name}]
**Classification**: {Wrong|Missing|New}
**Problem**: {what was wrong or missing}
**Pattern**: {failure category} — {N} traces affected
**Fix**: {what to change — be specific}

### [Section: {section_name}]
...
```

## Rules

- Be surgical: one change at a time.
- Don't rewrite the whole file.
- Reference the exact SKILL.md section heading you're modifying.
- Don't include the full diff — just describe what to add/modify/remove
  and where. The harness will apply the change.
- Prioritize changes that address the most frequent failure categories.
- If all sections look adequate, output "No changes needed" and explain
  why.
