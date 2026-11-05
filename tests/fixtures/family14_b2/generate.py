"""Build the frozen family-14 run of the 2.0.0b3 I1 pins (ADR-0037). Run by hand only.

From the repository root, on the commit *before* any 2.0.0b3 product change::

    .venv/bin/python -m tests.fixtures.family14_b2.generate

It runs `tests.family14_b2_scenario` twice in temporary directories, keeps the
values that both runs reproduce, refuses when a required role-input value is
not reproducible, and writes `run/` (from the first run, without `logs/`),
`corpus.jsonl` and `expected.json` beside this file. The history digests in
`expected.json` are what the 2.0.0b2 code computed while it ran; the pin
tests re-derive them from the committed bytes. Commit a rebuild only with the
reason the evidence had to change.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
MAX_TREE_BYTES = 1024 * 1024


def _writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)


def _remove_empty_directories(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def _run_once(scratch: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    import pytest

    from tests.family14_b2_scenario import (
        assert_i1_shape,
        capture_role_inputs,
        collect_pins,
        i1_options,
    )
    from tests.test_tree_campaign_family14_run import family14_run

    with pytest.MonkeyPatch.context() as patch:
        options = i1_options(scratch, patch)
        captured = capture_role_inputs(patch)
        run, snapshot = asyncio.run(family14_run(scratch, patch, **options))
    if snapshot.finished is None:
        raise AssertionError("the I1 scenario did not finish")
    assert_i1_shape(run)
    document = json.loads((scratch / "manifest.json").read_text(encoding="utf-8"))
    return run, {**collect_pins(run), **captured}, document


def main() -> None:
    import evolution
    from evolution.tree.campaign_editor import BRIEF_VERSION
    from tests.family14_b2_scenario import REQUIRED_REPRODUCIBLE, rederive_round_two_history

    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        run, pins, document = _run_once(Path(first))
        _, again, _ = _run_once(Path(second))
        reproducible = sorted(key for key in pins if pins[key] == again[key])
        missing = [key for key in REQUIRED_REPRODUCIBLE if key not in reproducible]
        if missing:
            raise AssertionError(f"not reproducible between two runs: {missing}")
        target = ROOT / "run"
        if target.exists():
            _writable(target)
            shutil.rmtree(target)
        shutil.copytree(run, target, ignore=shutil.ignore_patterns("logs"))
        shutil.copyfile(Path(first) / "corpus.jsonl", ROOT / "corpus.jsonl")
    _writable(target)
    _remove_empty_directories(target)
    readable = document["role_visibility"]["readable_paths"]
    # The b2 code's own round-2 histories (index 1: the second reflection and
    # the second round's base brief) must also be re-derivable from the bytes
    # just committed; a difference here means the re-derivation reader is wrong.
    rederived = rederive_round_two_history(target, ROOT / "corpus.jsonl", readable)
    if rederived != {
        "editor_history": pins["editor_history_digests"][1],
        "reflector_history": pins["reflector_history_digests"][1],
    }:
        raise AssertionError(f"committed bytes do not re-derive the b2 histories: {rederived}")
    expected = {
        **pins,
        "brief_version": BRIEF_VERSION,
        "producing_version": evolution.__version__,
        "reproducible": reproducible,
        "role_visibility": document["role_visibility"],
        "round_two_history": rederived,
    }
    (ROOT / "expected.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    usage = subprocess.run(["du", "-sb", str(ROOT)], check=True, capture_output=True, text=True)
    total = int(usage.stdout.split()[0])
    if total >= MAX_TREE_BYTES:
        raise AssertionError(f"the fixture is {total} bytes, not under {MAX_TREE_BYTES}")
    print(f"reproducible: {reproducible}")
    print("Markdown files to classify as test_fixture:")
    repository = ROOT.parents[2]
    for path in sorted(ROOT.rglob("*.md")):
        print(f"  {path.relative_to(repository)}")


if __name__ == "__main__":
    main()
