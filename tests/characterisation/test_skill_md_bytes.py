"""Characterisation lock: the exact bytes ``write_skill_md`` puts on disk.

SKILL.md is the on-disk representation of every evolved skill: version
snapshots, promotion diffs and candidate hashes are all taken over these
bytes. A refactor that moves the parser/store between packages must not
change a single byte of the serialisation, so this test pins the sha256 of
the written file for a set of in-memory inputs constructed here (never from
``.agents/``, which is gitignored user data).

Byte-level behaviour currently locked, all of it inherited from
``yaml.dump(fm_dict, default_flow_style=False, sort_keys=False)``:

- frontmatter key order follows ``SkillFrontmatter`` field order, not
  alphabetical order, and ``None`` fields are dropped (``exclude_none=True``);
- non-ASCII is escaped (``allow_unicode`` is *not* set) in the frontmatter but
  written verbatim in the body;
- the body is written through untouched, then exactly one ``\\n`` is appended;
- the separator between frontmatter and body is ``---\\n\\n``.

Round-tripping (``parse(write(x)) == x``) is asserted too, but it is the
weaker property: it survives reformatting that these hashes catch.

The hashes therefore also cover PyYAML's emitter (line folding at width 80,
``\\xNN``/``\\uNNNN`` escapes). ``pyproject.toml`` pins only ``pyyaml>=6.0``, so
a PyYAML upgrade that changes the emitter will trip this test as well — that is
intended: it would change every SKILL.md the framework writes.

To refresh a hash after an INTENTIONAL change to the serialisation::

    python - <<'EOF'
    import hashlib, tempfile, pathlib
    from tests.characterisation.test_skill_md_bytes import CASES, write_bytes
    with tempfile.TemporaryDirectory() as d:
        for name, (fm, body) in CASES.items():
            digest = hashlib.sha256(write_bytes(pathlib.Path(d), fm, body)).hexdigest()
            print(f'    "{name}": "{digest}",')
    EOF
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from evolution.core.models import SkillFrontmatter
from evolution.core.parser import load_skill, write_skill_md

# --- Inputs -----------------------------------------------------------------

MINIMAL = SkillFrontmatter(
    name="minimal-skill",
    description="Minimal skill used to lock the smallest possible SKILL.md.",
)
MINIMAL_BODY = "# Minimal Skill\n\nDo the thing, then stop."

EVOLVED = SkillFrontmatter(
    name="evolved-skill",
    description="Skill carrying evolution metadata: fitness and generation are strings.",
    license="Apache-2.0",
    metadata={
        "evolution-fitness": "0.8333333333333334",
        "evolution-generation": "7",
        "evolution-parent": "v6",
    },
)
EVOLVED_BODY = "# Evolved Skill\n\n## Steps\n\n1. Read the input.\n2. Emit the answer."

FULL = SkillFrontmatter(
    name="full-skill",
    description="Every optional field populated, including non-ASCII: naïve café ≤ 5 µs.",
    license="MIT",
    compatibility={"claude-code": ">=1.0", "notes": "requires bash"},
    metadata={"evolution-fitness": "1.0", "evolution-generation": "0", "unicode-value": "µ ≤ ∞"},
    allowed_tools="Read Write Bash",
)
FULL_BODY = "# Full Skill\n\nAll frontmatter fields present.\n"

TRICKY = SkillFrontmatter(
    name="tricky-body-skill",
    description="Body holds YAML-ish lines, a code fence, trailing whitespace and non-ASCII.",
)
TRICKY_BODY = (
    "# Tricky Body\n"
    "\n"
    "These lines look like frontmatter but are body text:\n"
    "\n"
    "name: not-the-real-name\n"
    "description: still just prose\n"
    "\n"
    "```yaml\n"
    "---\n"
    "name: nested-document\n"
    "---\n"
    "```\n"
    "\n"
    "This line ends in trailing whitespace ->   \n"
    "\tTab-indented line, then non-ASCII: naïve café — ≤ 5 µs.\n"
    "\n"
    "Trailing blank lines follow.\n"
    "   \n"
)

CASES: dict[str, tuple[SkillFrontmatter, str]] = {
    "minimal": (MINIMAL, MINIMAL_BODY),
    "evolution_metadata": (EVOLVED, EVOLVED_BODY),
    "full_frontmatter": (FULL, FULL_BODY),
    "tricky_body": (TRICKY, TRICKY_BODY),
}

# --- Golden values ----------------------------------------------------------

GOLDEN = {
    "minimal": "5248df30cd6284a97fbbb1f4f26f03c1174eaf27f91e93e7ff0c7b10a2155302",
    "evolution_metadata": "f22a7c34d5c8a2ab2bb7276be5c1ad2816acec1a42c3c37d8c89fdcdfd015f20",
    "full_frontmatter": "d80d8996d5d244401f5bc491395317fb121cba8a9c8044d80b8bd7014e9d1ceb",
    "tricky_body": "9a6485c4073cd3dce1059368f3cd6fc0f8d9b8d25bd30ed3e4a4dde939c4209f",
}


def write_bytes(root: Path, frontmatter: SkillFrontmatter, body: str) -> bytes:
    """Serialise one skill under *root* and return the file's raw bytes."""
    target = root / frontmatter.name / "SKILL.md"
    write_skill_md(target, frontmatter, body)
    return target.read_bytes()


# --- Tests ------------------------------------------------------------------


def test_every_case_has_a_golden_hash():
    """No case may be added/removed without touching the golden set."""
    assert set(CASES) == set(GOLDEN), "CASES and GOLDEN drifted apart"


def test_written_bytes_match_golden(tmp_path: Path):
    drift = {}
    for name, (frontmatter, body) in CASES.items():
        actual = hashlib.sha256(write_bytes(tmp_path, frontmatter, body)).hexdigest()
        if actual != GOLDEN[name]:
            drift[name] = actual
    assert not drift, f"SKILL.md serialisation changed; update GOLDEN in the same commit: {drift}"


def test_frontmatter_block_is_byte_exact(tmp_path: Path):
    """Spell out the minimal case so a hash mismatch is diagnosable."""
    written = write_bytes(tmp_path, MINIMAL, MINIMAL_BODY).decode("utf-8")
    assert written == (
        "---\n"
        "name: minimal-skill\n"
        "description: Minimal skill used to lock the smallest possible SKILL.md.\n"
        "---\n"
        "\n"
        "# Minimal Skill\n"
        "\n"
        "Do the thing, then stop.\n"
    )


def test_non_ascii_frontmatter_is_escaped_but_body_is_not(tmp_path: Path):
    """``yaml.dump`` runs without ``allow_unicode``; the body is passed through."""
    written = write_bytes(tmp_path, FULL, FULL_BODY).decode("utf-8")
    head, _, _ = written.partition("\n---\n\n")
    assert 'unicode-value: "\\xB5 \\u2264 \\u221E"' in head
    assert "µ ≤ ∞" not in head
    assert "naïve" not in head

    tricky = write_bytes(tmp_path, TRICKY, TRICKY_BODY).decode("utf-8")
    assert "naïve café — ≤ 5 µs." in tricky
    assert tricky.endswith(TRICKY_BODY + "\n")


def test_round_trip_reloads_the_same_skill(tmp_path: Path):
    """The weaker property: parse(write(x)) == x, modulo the parser's body strip."""
    for name, (frontmatter, body) in CASES.items():
        write_bytes(tmp_path, frontmatter, body)
        reloaded = load_skill(tmp_path / frontmatter.name)
        assert reloaded.frontmatter == frontmatter, name
        assert reloaded.body == body.strip(), name
