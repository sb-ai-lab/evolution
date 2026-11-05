"""Characterisation lock: which files a version snapshot is made of.

``VersionStore.save`` copies SKILL.md plus a hard-coded tuple of resource
subdirectories into ``.evolution/versions/<label>/``. Nothing else in the skill
directory is snapshotted, and nothing is re-serialised on the way in (it is a
``shutil.copy2``/``copytree``, so bytes survive verbatim). The upcoming merge
parameterises snapshotting by artefact kind; if that member set silently gains
or loses an entry, every stored version changes shape and ``evo promote`` /
``evo diff`` start comparing different things across generations.

Four things are locked here:

1. ``GOLDEN_MANAGED_ENTRIES`` — the value of ``_MANAGED_ENTRIES`` in
   ``lineage/version.py``, read back out of the source with ``ast`` so an edit to
   the member set fails here. The snapshot loop must keep iterating that constant
   rather than re-inlining a literal beside it.
2. ``GOLDEN_LITERAL_SITES`` — every module under ``src/evolution`` that still
   repeats the bare resource-subdir literal, and how often. A merge that
   parameterises one snapshot site and forgets its copies fails loudly instead of
   drifting apart.
3. ``GOLDEN_SNAPSHOT_FILES`` — the observed result of a real ``save`` call: the
   exact set of relative paths that land in the version directory, and the
   sha256 of each one's bytes.
4. That snapshotting is a *byte copy* and not a re-serialisation. This only has
   teeth because the fixture SKILL.md below is deliberately **not**
   round-trippable through ``load_skill`` + ``write_skill_md``: ``description``
   comes before ``name``, it is quoted, ``metadata`` is written in inline flow
   style, the body contains a CRLF, and the body ends with an extra blank line.
   Every one of those is normalised away by ``write_skill_md``, so swapping the
   ``shutil.copy2`` in ``VersionStore.save`` for a re-serialisation changes the
   SKILL.md digest and fails ``test_snapshot_bytes_match_golden``.
   ``test_fixture_skill_md_is_not_round_trippable`` guards that property of the
   fixture itself, so a later edit cannot quietly disarm the byte lock.

Behaviour deliberately pinned by the fixture below: empty resource dirs *are*
snapshotted, nested files under a resource dir are copied, ``promote`` restores
the whole managed tree rather than SKILL.md alone, and everything else in the
skill directory — ``.traces/``, ``.evolution/``, ``evals/``, unknown dirs, loose
top-level files — is left out.

The first two of those reversed since this lock was archived, in both cases
because HEAD fixed a real asymmetry. They are re-pinned in the new direction,
with the old behaviour named in each test's docstring, so the fix cannot be
undone silently by the merge.

Source-text assertions (1 and 2) are anchored to *this* checkout, derived from
this file's own location, not to whatever ``import evolution`` happens to
resolve to; ``test_imported_package_is_this_checkout`` fails loudly when the two
disagree, because in that case the runtime goldens and the source-text goldens
would be describing different repositories.

To refresh the file hashes after an INTENTIONAL change::

    python - <<'EOF'
    import pathlib, tempfile
    from tests.characterisation.test_snapshot_members import snapshot_digests
    with tempfile.TemporaryDirectory() as d:
        for path, digest in snapshot_digests(pathlib.Path(d)).items():
            print(f'    "{path}": "{digest}",')
    EOF

``GOLDEN_SNAPSHOT_SUBDIRS`` and ``GOLDEN_LITERAL_SITES`` are edited by hand, in
the same commit as the source change they describe.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import datetime
from pathlib import Path

import pytest

import evolution
from evolution.core.models import LineageEntry
from evolution.core.parser import load_skill, write_skill_md
from evolution.core.store import SkillStore
from evolution.lineage.tracker import LineageTracker
from evolution.lineage.version import VersionStore

#: The checkout this test file lives in. Deliberately NOT derived from
#: ``evolution.__file__``: a stray ``PYTHONPATH``/editable install pointing at a
#: sibling checkout would otherwise make the ``ast`` assertions below describe
#: someone else's source while still passing.
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "evolution"
_IMPORTED_ROOT = Path(evolution.__file__).resolve().parent
_VERSION_SOURCE = _SRC_ROOT / "lineage" / "version.py"

# --- Inputs -----------------------------------------------------------------

SKILL_NAME = "snapshot-members"

#: Deliberately NOT round-trippable through ``load_skill`` + ``write_skill_md``
#: (see module docstring): key order, quoting, flow style, CRLF and the trailing
#: blank line are all normalised by the writer. That is what makes
#: ``test_snapshot_bytes_match_golden`` able to tell a copy from a rewrite.
SKILL_MD = (
    "---\n"
    'description: "Fixture skill pinning the version snapshot member set."\n'
    "name: snapshot-members\n"
    'metadata: {evolution-generation: "2"}\n'
    "---\n"
    "\n"
    "# Snapshot Members\n"
    "\n"
    "Body bytes must reach the snapshot verbatim.\r\n"
    "\n"
)

#: Files written into the working skill directory before ``save`` is called.
#: The first four are expected to be snapshotted; the rest must not be.
SKILL_TREE: dict[str, str] = {
    "SKILL.md": SKILL_MD,
    "scripts/run.py": "print('scripts')\n",
    "scripts/nested/deep.py": "print('nested')\n",
    "references/guide.md": "# Guide\n",
    "assets/logo.txt": "logo\n",
    ".traces/index.json": '{"traces": []}\n',
    ".evolution/lineage.json": '{"lineage": []}\n',
    "evals/evals.json": '{"evals": []}\n',
    "junk/leftover.txt": "leftover\n",
    "notes.txt": "loose top-level file\n",
}

EXCLUDED_TOP_LEVEL = (".traces", ".evolution", "evals", "junk", "notes.txt")

# --- Golden values ----------------------------------------------------------

#: The whole managed member set, as the named constant in ``lineage/version.py``.
GOLDEN_MANAGED_ENTRIES = ("SKILL.md", "scripts", "references", "assets")

#: The resource subdirs alone — the part still copy-pasted as a bare literal.
GOLDEN_SNAPSHOT_SUBDIRS = GOLDEN_MANAGED_ENTRIES[1:]

GOLDEN_LITERAL_SITES = {
    "core/store.py": 4,
    "evolve/optimizer.py": 1,
}

GOLDEN_SNAPSHOT_FILES = {
    "SKILL.md": "3db6311bee2af04371436100301aef9325be4f6c742fcbb54a6bec4b4ba5327f",
    "assets/logo.txt": "84e68693496e281178406d280fe930ba381918a2d8267fa3e43c894c40be93e2",
    "references/guide.md": "bc553ffe57e544498b12a9865dbf3abc2004c474e349c52c378eaa402287424b",
    "scripts/nested/deep.py": "0a388e8c2aff7aa01c6db0ec52c291f82a9168c980fb95c246cd612e116377c7",
    "scripts/run.py": "6267f263f76ace6219ab130b1151c419c27ff1c63930cd963556bc78ecf54deb",
}


def write_fixture_file(path: Path, content: str) -> None:
    """Write *content* verbatim: ``newline=""`` keeps the fixture's CRLF intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


def build_skill_dir(root: Path) -> Path:
    """Materialise the fixture skill under *root* and return its directory."""
    skill_dir = root / SKILL_NAME
    for relpath, content in SKILL_TREE.items():
        write_fixture_file(skill_dir / relpath, content)
    return skill_dir


def snapshot_digests(root: Path) -> dict[str, str]:
    """Snapshot the fixture skill and return ``relpath -> sha256`` of the result."""
    version_dir = VersionStore(load_skill(build_skill_dir(root))).save("v1")
    return {
        path.relative_to(version_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(version_dir.rglob("*"))
        if path.is_file()
    }


def _string_tuple(node: ast.AST) -> tuple[str, ...] | None:
    """Return the value of a tuple-of-string-literals node, else ``None``."""
    if not isinstance(node, ast.Tuple):
        return None
    values = []
    for element in node.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        values.append(element.value)
    return tuple(values)


def _managed_entries_constant() -> tuple[str, ...] | None:
    """The value of ``_MANAGED_ENTRIES`` read out of ``lineage/version.py``."""
    tree = ast.parse(_VERSION_SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if "_MANAGED_ENTRIES" in names:
            return _string_tuple(node.value)
    return None


def _member_loop_iterables() -> set[str]:
    """Source text of every ``for`` in ``version.py`` that walks the member set.

    A loop qualifies if it names ``_MANAGED_ENTRIES`` or iterates a bare tuple of
    string literals — the two shapes the member set can take. Anything else in
    the module is unrelated and ignored.
    """
    source = _VERSION_SOURCE.read_text(encoding="utf-8")
    iterables = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.For):
            continue
        segment = ast.get_source_segment(source, node.iter) or ""
        if "_MANAGED_ENTRIES" in segment or _string_tuple(node.iter) is not None:
            iterables.add(segment)
    return iterables


def _literal_sites() -> dict[str, int]:
    """``relpath -> occurrences`` of the golden tuple across ``src/evolution``."""
    sites: dict[str, int] = {}
    for module in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        hits = sum(_string_tuple(node) == GOLDEN_SNAPSHOT_SUBDIRS for node in ast.walk(tree))
        if hits:
            sites[module.relative_to(_SRC_ROOT).as_posix()] = hits
    return sites


@pytest.fixture
def snapshot(tmp_path: Path) -> dict[str, str]:
    return snapshot_digests(tmp_path)


# --- Tests ------------------------------------------------------------------


def test_imported_package_is_this_checkout():
    """Source-text goldens and runtime goldens must describe the same tree."""
    assert _IMPORTED_ROOT == _SRC_ROOT, (
        "this test is running against a different checkout than the one it lives in: "
        f"`import evolution` resolved to {_IMPORTED_ROOT}, but this test file belongs to "
        f"{_SRC_ROOT}. The ast/source-text goldens would describe one tree while the "
        "snapshot goldens describe another. Fix the interpreter/PYTHONPATH, do not "
        "'update' the goldens."
    )


def test_managed_entry_constant_matches_golden():
    """(a) The member list, now a named constant rather than an inline literal."""
    assert _managed_entries_constant() == GOLDEN_MANAGED_ENTRIES, (
        "_MANAGED_ENTRIES in lineage/version.py changed; update "
        "GOLDEN_MANAGED_ENTRIES in the same commit"
    )


def test_snapshot_iterates_the_named_constant_rather_than_a_literal():
    """The single definition must stay single: no loop may re-inline the members.

    An inline ``for subdir in ("scripts", ...)`` here is how the member set drifts
    away from ``_MANAGED_ENTRIES`` without any test noticing.
    """
    assert _member_loop_iterables() == {"_MANAGED_ENTRIES", "_MANAGED_ENTRIES[1:]"}


def test_subdir_literal_sites_match_golden():
    """The same literal is copy-pasted; a merge must not update only some sites."""
    actual = _literal_sites()
    assert actual == GOLDEN_LITERAL_SITES, (
        "sites repeating the snapshot subdir tuple changed; update "
        f"GOLDEN_LITERAL_SITES in the same commit: {actual}"
    )


def test_snapshot_holds_exactly_the_golden_files(snapshot: dict[str, str]):
    """(b) No file may be added to or dropped from a version snapshot silently."""
    assert set(snapshot) == set(GOLDEN_SNAPSHOT_FILES), (
        f"version snapshot membership changed; update GOLDEN_SNAPSHOT_FILES: {snapshot}"
    )


def test_fixture_skill_md_is_not_round_trippable(tmp_path: Path):
    """Arm the byte lock: re-serialising the fixture must change its bytes.

    If this ever passes trivially — i.e. ``write_skill_md`` starts reproducing
    ``SKILL_MD`` verbatim — then ``test_snapshot_bytes_match_golden`` can no
    longer tell ``shutil.copy2`` from a rewrite, and SKILL_MD must be made
    awkward again rather than left as a lock that only looks strict.
    """
    skill = load_skill(build_skill_dir(tmp_path))
    rewritten = tmp_path / "rewritten" / "SKILL.md"
    write_skill_md(rewritten, skill.frontmatter, skill.body)
    assert rewritten.read_bytes() != SKILL_MD.encode("utf-8"), (
        "the fixture SKILL.md now survives load_skill + write_skill_md unchanged, so "
        "test_snapshot_bytes_match_golden would pass even if VersionStore.save "
        "re-serialised instead of copying; restore a non-round-trippable shape "
        "(key order, quoting, flow-style metadata, CRLF, trailing blank line)"
    )


def test_snapshot_bytes_match_golden(snapshot: dict[str, str]):
    """Every snapshotted file's bytes, so a re-serialisation cannot pass as a copy.

    Scope: this compares the bytes of the files that are *present*; membership
    (a file added or dropped) is asserted by
    ``test_snapshot_holds_exactly_the_golden_files``, not here.
    """
    drift = {
        path: digest
        for path, digest in snapshot.items()
        if digest != GOLDEN_SNAPSHOT_FILES.get(path)
    }
    assert not drift, f"snapshot file bytes changed; update GOLDEN_SNAPSHOT_FILES: {drift}"


def test_excluded_entries_are_not_snapshotted(tmp_path: Path):
    """Spell out the exclusions so a membership failure is diagnosable."""
    version_dir = VersionStore(load_skill(build_skill_dir(tmp_path))).save("v1")
    present = sorted(path.name for path in version_dir.iterdir())
    for excluded in EXCLUDED_TOP_LEVEL:
        assert excluded not in present, f"{excluded} leaked into the snapshot"
    assert present == ["SKILL.md", *sorted(GOLDEN_SNAPSHOT_SUBDIRS)]


def test_empty_resource_dirs_are_snapshotted(tmp_path: Path):
    """An empty ``scripts/`` is preserved, not dropped.

    The archived lock asserted the opposite, against an older ``save`` that
    guarded the copy with ``any(src_dir.iterdir())``. HEAD keys off
    ``src_dir.is_dir()`` alone, so "the skill has an empty scripts/" and "the
    skill has no scripts/" are now distinguishable across a save/restore round
    trip. Locked in the new direction deliberately.
    """
    skill_dir = tmp_path / SKILL_NAME
    skill_dir.mkdir()
    write_fixture_file(skill_dir / "SKILL.md", SKILL_MD)
    for subdir in GOLDEN_SNAPSHOT_SUBDIRS:
        (skill_dir / subdir).mkdir()

    version_dir = VersionStore(load_skill(skill_dir)).save("v1")

    assert sorted(path.name for path in version_dir.iterdir()) == sorted(GOLDEN_MANAGED_ENTRIES)


def test_promote_restores_the_whole_managed_tree(tmp_path: Path):
    """``promote`` restores every managed entry, not SKILL.md alone.

    The archived lock characterised an asymmetry — ``save`` stored the resource
    dirs while ``promote`` copied back only SKILL.md, so promoting stranded the
    working copy's ``scripts/`` at whatever the last local edit left. HEAD swaps
    the full managed tree (``_swap_managed_tree`` over ``_MANAGED_ENTRIES``), so
    save and restore now cover the same member set. Locked in the new direction:
    this asymmetry must not come back.
    """
    store = SkillStore(project_dir=tmp_path)
    store.create(name=SKILL_NAME, description="Fixture skill pinning restore behaviour.")
    skill_dir = store.get(SKILL_NAME).path
    (skill_dir / "scripts").mkdir(exist_ok=True)
    (skill_dir / "scripts" / "run.py").write_text("print('v2')\n", encoding="utf-8")

    tracker = LineageTracker(store.get(SKILL_NAME))
    VersionStore(store.get(SKILL_NAME)).save("v2")
    tracker.record(
        LineageEntry(
            version="v2",
            parent="v1",
            timestamp=datetime.now(),
            origin="manual",
            mutation_type="manual-edit",
            status="candidate",
        )
    )
    tracker.promote("v2")

    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text("print('local edit')\n", encoding="utf-8")

    LineageTracker(store.get(SKILL_NAME)).promote("v2")

    assert "local edit" not in skill_md.read_text(encoding="utf-8")
    assert (skill_dir / "scripts" / "run.py").read_text(encoding="utf-8") == "print('v2')\n"
