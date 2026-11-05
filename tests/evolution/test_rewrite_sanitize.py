"""Tests for `_sanitize_rewritten_body` — narrow Codex rewrite-body sanitizer.

The sanitizer must:
* strip a SINGLE whole-body enclosing code fence (only when the entire output
  is one fenced block),
* strip a leading conversational preamble before the real body,
* strip a trailing appended "## Summary of changes" / "## Changes made"
  changelog section,
* PRESERVE internal example fences verbatim (highest-risk regression),
* raise ValueError on empty / still-preamble / <40% original length.
"""

from __future__ import annotations

import pytest

from evolution.evolve.rewrite_sanitize import sanitize_rewritten_body as _sanitize_rewritten_body

# A reasonably long, realistic skill body used as the "original" so that the
# 40%-length guard does not trip for legitimate outputs.
ORIGINAL_BODY = """---
name: demo-skill
description: A demo skill for sanitizer tests
---

# Demo Skill

This skill explains how to process spreadsheets carefully.

## Approach

Always validate inputs before writing outputs.

```python
import openpyxl

wb = openpyxl.load_workbook("in.xlsx")
ws = wb.active
for row in ws.iter_rows():
    print(row)
```

## Notes

Be careful about types and encodings when reading files.
""".strip()


# A clean, fence-less body used to exercise the ONLY unambiguous wrapper
# cases that may safely be stripped.
_FENCELESS_BODY = """---
name: fenceless-skill
description: A skill body with no internal fenced code blocks
---

# Fenceless Skill

This skill explains how to validate spreadsheet inputs in prose only.

## Approach

Always validate inputs before writing outputs. Reject malformed rows
and log the rejected count so the operator can audit data quality
before the cleaned file is consumed by downstream reporting jobs.

## Notes

Be careful about types and encodings when reading files. Never leave a
partially-written output on disk.""".strip()


def test_whole_body_enclosing_fence_with_internal_fence_unchanged():
    """A ```markdown-wrapped body that ALSO contains an internal ```python
    fence is information-theoretically indistinguishable from a no-wrapper
    body that merely opens with a ```markdown example and closes with a
    fenced example. It is therefore returned UNCHANGED.

    This is a DELIBERATE conservative trade-off (under-strip), NOT a
    regression: previously (commit ef1298b) this stripped the outer two
    lines, which is unsafe because the identical token structure also
    describes a real skill body — stripping there would orphan content and
    leave an unterminated code block (byte corruption). Leaving a stray
    cosmetic outer wrapper is the acceptable price for never corrupting a
    real skill.
    """
    raw = "```markdown\n" + ORIGINAL_BODY + "\n```"
    out = _sanitize_rewritten_body(raw, ORIGINAL_BODY)
    # Returned byte-identical (only outer .strip()).
    assert out == raw.strip()
    # The (stray, cosmetic) outer wrapper is intentionally still present.
    assert out.lstrip().startswith("```markdown")
    # Interior bytes untouched.
    assert "# Demo Skill" in out
    assert "import openpyxl" in out
    assert "```python" in out


def test_true_fenceless_markdown_wrapper_stripped():
    """A TRUE single ```markdown wrapper around a body with NO internal
    fences -> outer two lines removed, interior byte-identical. This is the
    unambiguous wrapper that still works.
    """
    raw = "```markdown\n" + _FENCELESS_BODY + "\n```"
    out = _sanitize_rewritten_body(raw, _FENCELESS_BODY)
    assert out == _FENCELESS_BODY.strip()
    assert not out.lstrip().startswith("```")
    assert out.lstrip().startswith("---")


def test_true_fenceless_bare_wrapper_stripped():
    """A TRUE bare ``` wrapper around a fence-less body -> stripped, interior
    byte-identical.
    """
    raw = "```\n" + _FENCELESS_BODY + "\n```"
    out = _sanitize_rewritten_body(raw, _FENCELESS_BODY)
    assert out == _FENCELESS_BODY.strip()
    assert out.strip().splitlines()[0].strip() != "```"
    assert out.lstrip().startswith("---")


def test_leading_preamble_removed():
    """Conversational preamble before the real heading is stripped."""
    raw = "Here is the updated skill:\n\n" + ORIGINAL_BODY
    out = _sanitize_rewritten_body(raw, ORIGINAL_BODY)
    assert not out.lstrip().lower().startswith("here is the updated skill")
    # Body starts at real frontmatter / heading content.
    assert out.lstrip().startswith("---") or out.lstrip().startswith("# Demo Skill")


def test_internal_example_fence_preserved():
    """A clean body with an internal ```python fence must be untouched."""
    out = _sanitize_rewritten_body(ORIGINAL_BODY, ORIGINAL_BODY)
    assert "```python" in out
    assert "import openpyxl" in out
    assert 'wb = openpyxl.load_workbook("in.xlsx")' in out
    # Idempotent: only strip()-level difference from the input.
    assert out == ORIGINAL_BODY.strip()


# Corruption-regression for the review probe: a NO-WRAPPER body that
# legitimately OPENS with a ```markdown frontmatter-authoring example and
# ENDS with a ```python example, prose between. This is the EXACT token
# structure of a true ```markdown wrapper around a body containing a
# ```python block — they are indistinguishable. At commit ef1298b the old
# rule deleted line 1 (```markdown) and the final line (```), orphaning the
# frontmatter example and leaving an unterminated python block: a
# byte-corrupted skill. It MUST now be returned byte-identical.
_MARKDOWN_AUTHORING_NO_WRAPPER = """```markdown
---
name: example-skill
description: Authored by the user following this guide
---
```

# SKILL.md Authoring Skill

This skill teaches how to author a well-formed SKILL.md. The block above
shows the exact YAML frontmatter shape every skill file must begin with.

## Frontmatter rules

The `name` must be kebab-case and the `description` must be a single
sentence. Do not add fields the specification does not define.

## Worked example

A minimal end-to-end body looks like this:

```python
import pathlib

text = pathlib.Path("SKILL.md").read_text()
assert text.startswith("---")
```""".strip()


def test_markdown_authoring_no_wrapper_byte_identical():
    """Review-probe corruption shape: a markdown-authoring skill that opens
    with a ```markdown frontmatter example and ends with a ```python example.

    Indistinguishable from a true wrapper -> returned BYTE-IDENTICAL. (Fails
    at ef1298b — old rule stripped the opening ```markdown and trailing ```,
    corrupting the body; passes after the ambiguity gate is added.)
    """
    out = _sanitize_rewritten_body(_MARKDOWN_AUTHORING_NO_WRAPPER, _MARKDOWN_AUTHORING_NO_WRAPPER)
    assert out == _MARKDOWN_AUTHORING_NO_WRAPPER.strip()
    # Opening frontmatter example intact (NOT orphaned).
    assert out.splitlines()[0].strip() == "```markdown"
    assert "name: example-skill" in out
    # Closing python example intact, fully fenced.
    assert "```python" in out
    assert 'assert text.startswith("---")' in out
    # All four fence markers survive (2 blocks, fully terminated).
    assert out.count("```") == 4


def test_whole_body_fence_and_internal_fence_together_unchanged():
    """Same ambiguous shape as the review probe (```markdown wrapper around a
    body containing a ```python block). Now asserts the conservative
    contract: returned UNCHANGED.

    Deliberate safe trade-off (under-strip), NOT a regression — see
    test_whole_body_enclosing_fence_with_internal_fence_unchanged. Stripping
    here is information-theoretically impossible to do safely because the
    identical token stream also describes a real skill body.
    """
    raw = "```markdown\n" + ORIGINAL_BODY + "\n```"
    out = _sanitize_rewritten_body(raw, ORIGINAL_BODY)
    # Returned byte-identical; the cosmetic outer wrapper stays.
    assert out == raw.strip()
    assert out.lstrip().startswith("```markdown")
    # Internal example fence still present exactly once, content intact.
    assert out.count("```python") == 1
    assert "import openpyxl" in out


def test_trailing_changelog_section_removed():
    """A trailing '## Summary of changes' appended block is removed."""
    raw = (
        ORIGINAL_BODY
        + "\n\n## Summary of changes\n\n"
        + "- Added validation step\n- Clarified the notes section\n"
    )
    out = _sanitize_rewritten_body(raw, ORIGINAL_BODY)
    assert "Summary of changes" not in out
    assert "Added validation step" not in out
    # Real body retained.
    assert "# Demo Skill" in out
    assert "## Notes" in out


def test_trailing_changes_made_section_removed():
    raw = ORIGINAL_BODY + "\n\n## Changes Made\n\n- tweaked stuff\n"
    out = _sanitize_rewritten_body(raw, ORIGINAL_BODY)
    assert "Changes Made" not in out
    assert "tweaked stuff" not in out
    assert "# Demo Skill" in out


def test_mid_document_summary_section_preserved():
    """A legitimate '## Summary' that is NOT the trailing section survives."""
    body = (
        "# Title\n\n## Summary\n\nThis skill summarizes things.\n\n"
        "## Steps\n\n1. Do the thing.\n2. Done.\n"
    )
    out = _sanitize_rewritten_body(body, body)
    assert "## Summary" in out
    assert "This skill summarizes things." in out
    assert "## Steps" in out


def test_empty_output_raises():
    with pytest.raises(ValueError):
        _sanitize_rewritten_body("", ORIGINAL_BODY)
    with pytest.raises(ValueError):
        _sanitize_rewritten_body("   \n\t  \n", ORIGINAL_BODY)


def test_still_preamble_only_raises():
    with pytest.raises(ValueError):
        _sanitize_rewritten_body("Sure, done!", ORIGINAL_BODY)


def test_short_complete_output_is_accepted():
    """Complete concise rewrites may be much shorter than the original."""
    short = "# Demo Skill\n\nok."
    assert len(short) < 0.40 * len(ORIGINAL_BODY)
    assert _sanitize_rewritten_body(short, ORIGINAL_BODY) == short


def test_clean_body_idempotent():
    """A normal clean body with no fence/preamble/changelog is ~unchanged."""
    out = _sanitize_rewritten_body(ORIGINAL_BODY, ORIGINAL_BODY)
    assert out == ORIGINAL_BODY.strip()
    # Running it twice is stable.
    assert _sanitize_rewritten_body(out, ORIGINAL_BODY) == out


# ---------------------------------------------------------------------------
# Adversarial cases (regression for the Task 7 correctness defects).
# Each of these CORRUPTED a realistic evolved body at commit f02be45.
# ---------------------------------------------------------------------------

# A body long enough that the 40% guard never trips, that genuinely OPENS with
# a code example and ENDS with a (different) code example, prose between. These
# are TWO separate top-level fences — NOT a single wrapper — and must survive
# byte-for-byte.
_OPENS_AND_CLOSES_WITH_FENCE = """```python
import pandas as pd

df = pd.read_csv("input.csv")
print(df.head())
```

# Data Processing Skill

This skill shows how to load, validate and re-serialize tabular data
without losing precision. Always check dtypes before writing.

## Validation

Validate every column against the expected schema. Reject rows that do
not conform and log the rejected count for the operator.

## Worked example

The final, recommended end-to-end snippet:

```python
import pandas as pd

df = pd.read_csv("input.csv")
df = df.dropna()
df.to_parquet("out.parquet", index=False)
```""".strip()


def test_A3_opens_and_closes_with_separate_python_fences_unchanged():
    """Body opens with one ```python example and ends with another.

    These are two SEPARATE top-level fences, not a single wrapper. The body
    must be returned byte-identical (no fence stripped, no corruption).
    """
    out = _sanitize_rewritten_body(_OPENS_AND_CLOSES_WITH_FENCE, _OPENS_AND_CLOSES_WITH_FENCE)
    assert out == _OPENS_AND_CLOSES_WITH_FENCE.strip()
    # First example opener intact.
    assert out.splitlines()[0].strip() == "```python"
    # Both python blocks present, both fully fenced (4 fence markers total).
    assert out.count("```python") == 2
    assert out.count("```") == 4
    assert 'df.to_parquet("out.parquet", index=False)' in out


_OPENS_TEXT_CLOSES_PYTHON = """```text
Input layout:
  col_a, col_b, col_c
```

# Harmonization Skill

Normalize lab units to SI before any downstream comparison. The mapping
table below is authoritative; do not infer units from column names.

## Procedure

1. Detect the source unit.
2. Apply the conversion factor.
3. Round to the agreed precision.

```python
def to_si(value, unit):
    return value * FACTORS[unit]
```""".strip()


def test_A4_opens_text_closes_python_unchanged():
    """Opens with a ```text block, ends with a ```python block, prose between.

    Two distinct top-level fences with differing info strings -> unchanged.
    """
    out = _sanitize_rewritten_body(_OPENS_TEXT_CLOSES_PYTHON, _OPENS_TEXT_CLOSES_PYTHON)
    assert out == _OPENS_TEXT_CLOSES_PYTHON.strip()
    assert out.splitlines()[0].strip() == "```text"
    assert "```python" in out
    assert "def to_si(value, unit):" in out
    assert out.count("```") == 4


def test_B_markdown_wrapper_with_internal_fences_unchanged():
    """A ```markdown wrapper around a body with internal ```python blocks.

    This is the SAME information-theoretically-impossible shape as the review
    probe: the token stream is identical to a no-wrapper body that opens with
    a ```markdown example and closes with a ```python example. Under the
    conservative contract it is returned UNCHANGED — a deliberate safe
    trade-off (under-strip), NOT a regression. Leaving the stray cosmetic
    outer wrapper is acceptable; stripping it would corrupt the
    indistinguishable real-skill case. (Old assertion: outer stripped, inner
    preserved — that behavior cannot be implemented without also corrupting a
    real skill body of identical structure.)
    """
    inner = """# Pipeline Skill

Build the assembly query carefully.

## Step 1

```python
import duckdb
con = duckdb.connect()
```

## Step 2

```python
con.execute("CREATE TABLE t AS SELECT * FROM read_csv('a.csv')")
```

## Notes

Always close the connection."""
    raw = "```markdown\n" + inner + "\n```"
    out = _sanitize_rewritten_body(raw, inner)
    # Returned byte-identical; the cosmetic outer wrapper intentionally stays.
    assert out == raw.strip()
    assert out.lstrip().startswith("```markdown")
    # All interior bytes (including both internal fences) untouched.
    assert out.count("```python") == 2
    assert out.count("```") == 6  # outer pair + two internal python blocks
    assert "import duckdb" in out
    assert "con.execute(\"CREATE TABLE t AS SELECT * FROM read_csv('a.csv')\")" in out
    assert "Always close the connection." in out


_REAL_CHANGES_SECTION = """# Config Updater Skill

This skill applies user-requested configuration edits safely.

## Overview

Parse the existing config, apply the requested edits, validate the
result against the schema, then write it back atomically.

## Steps

1. Load the config file.
2. Apply edits.
3. Validate.
4. Write back.

## Changes

When the user requests CHANGES to the configuration, treat each change
as an independent operation. Apply additive changes first, then
removals, then modifications. After every change, re-run schema
validation. If any change makes the config invalid, roll back the
entire batch and report which change failed. Never leave the config in
a partially-applied state, because downstream services poll it
continuously and a half-written change can crash them. Document each
applied change in the audit log with a timestamp and the requesting
user so that operators can trace configuration drift over time.""".strip()


def test_D2_real_final_changes_section_preserved():
    """A substantive operational '## Changes' section that is the final
    second-half heading must be preserved (NOT truncated as a changelog).
    """
    out = _sanitize_rewritten_body(_REAL_CHANGES_SECTION, _REAL_CHANGES_SECTION)
    assert "## Changes" in out
    assert "treat each change\nas an independent operation" in out
    assert "roll back the\nentire batch" in out
    assert out == _REAL_CHANGES_SECTION.strip()


_REAL_WHAT_CHANGED_SECTION = """# Schema Migration Skill

Apply forward-only schema migrations to the analytics warehouse.

## Approach

Each migration is a numbered SQL file. Apply them in order, inside a
transaction, recording the applied version in a migrations table.

## Procedure

1. Read the current schema version.
2. Apply each pending migration in order.
3. Update the migrations table.

## What changed

When a migration changes a column type, you must backfill existing
rows before the new constraint is enforced. Compute the converted
value in a temporary column, verify row counts match, swap the
columns, then drop the old one. For wide tables this can take hours,
so run it in batches of fifty thousand rows and commit between
batches to avoid holding long locks. If a backfill is interrupted,
the migration must be resumable from the last committed batch, so
always record progress in the migrations table after each batch.""".strip()


def test_D6_real_what_changed_section_preserved():
    """A substantive '## What changed' operational section is preserved."""
    out = _sanitize_rewritten_body(_REAL_WHAT_CHANGED_SECTION, _REAL_WHAT_CHANGED_SECTION)
    assert "## What changed" in out
    assert "you must backfill existing\nrows" in out
    assert "resumable from the last committed batch" in out
    assert out == _REAL_WHAT_CHANGED_SECTION.strip()


def test_genuine_trailing_changelog_still_stripped():
    """A genuine short bullet-list '## Summary of changes' / '## Changelog'
    appended at the very end is still removed.
    """
    raw_a = (
        ORIGINAL_BODY
        + "\n\n## Summary of changes\n\n"
        + "- Added a validation step\n- Clarified the notes section\n"
        + "- Fixed the openpyxl import\n"
    )
    out_a = _sanitize_rewritten_body(raw_a, ORIGINAL_BODY)
    assert "Summary of changes" not in out_a
    assert "Added a validation step" not in out_a
    assert "# Demo Skill" in out_a
    assert "## Notes" in out_a

    raw_b = (
        ORIGINAL_BODY + "\n\n## Changelog\n\n" + "- Tweaked the approach\n- Reworded the notes\n"
    )
    out_b = _sanitize_rewritten_body(raw_b, ORIGINAL_BODY)
    assert "## Changelog" not in out_b
    assert "Tweaked the approach" not in out_b
    assert "# Demo Skill" in out_b


_PREAMBLE_CONTENT_WORD_BODY = """The improved skill keeps the same structure but adds a validation step.

# Spreadsheet Skill

Process spreadsheets carefully and validate every input.

## Overview

This skill reads a workbook, validates the data, and writes a clean
copy. The validation step is the important addition: it rejects rows
with missing required fields and logs the rejected count so the
operator can audit data quality before the cleaned file is consumed
downstream by reporting jobs that assume well-formed input.

## Steps

1. Load the workbook.
2. Validate every row.
3. Write the cleaned copy.""".strip()


def test_X5_content_word_first_sentence_preserved():
    """First sentence 'The improved skill keeps the same structure but adds a
    validation step.' is real body prose, NOT a conversational preamble.
    It must be preserved even though it contains 'improved skill'.
    """
    out = _sanitize_rewritten_body(_PREAMBLE_CONTENT_WORD_BODY, _PREAMBLE_CONTENT_WORD_BODY)
    assert out.startswith("The improved skill keeps the same structure but adds a validation step.")
    assert out == _PREAMBLE_CONTENT_WORD_BODY.strip()


def test_conversational_preamble_still_stripped():
    """A genuine conversational preamble line is still removed."""
    raw = "Here is the updated skill:\n\n" + ORIGINAL_BODY
    out = _sanitize_rewritten_body(raw, ORIGINAL_BODY)
    assert not out.lstrip().lower().startswith("here is the updated skill")
    assert out.lstrip().startswith("---") or out.lstrip().startswith("# Demo Skill")
