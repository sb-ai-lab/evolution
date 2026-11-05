"""`reflect_change_fingerprint/1` and the change description."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from evolution.tree.artifacts.artifact_policy import Entry
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.roles.reflector import reflect_change
from evolution.tree.roles.reflector.reflect_change import (
    PathChange,
    Side,
    change_record,
    cut_to_literal,
    describe,
    excerpt_screen,
    fingerprint,
    path_change,
    path_display,
)
from evolution.tree.roles.reflector_contracts import oracle_needles

# Golden digests, computed from the record definition with an
# independent script while planning (2026-10-16). A different value means the
# record, its canonical JSON or the matcher changed: never update a value here,
# introduce a new scheme instead.
GOLDEN = {
    "edit_e": "sha256:65417bed8d221a85dc67ce140f4f0b62ecd15b1eef5abdd258358fccb9e8a088",
    "created": "sha256:30e8ec06dc2e84715bd454facafbcdfd57981a905c4e2d10314435ec93d3bb4a",
    "deleted": "sha256:641fbf8afd3165816db22daad4afb9f45e400fd00c2822ded385023717dff552",
    "exec_only": "sha256:d4bc486f82ab35be6da147d690cdb9ef7c49cfbc6568bd1403faa32b334c0e7e",
    "terminators_only": "sha256:2b71cc23b63d10b5ca25d149730f63c3b11968f7ff1c9e0540ce25baece9629e",
    "binary": "sha256:9b5a54f95730297d1992b0dc5a06d2b80cbb06a9ae1c1a7617d1d0d619afeff8",
    "directory_created": "sha256:0d14f6f2dd982baa73fc36ea49f025052e672740d67adc1269383c81e856dcb3",
    "text_to_binary": "sha256:1828529d9bbb3d8e2ef6f9fe3d9bd9c35c86cfe00b7f1ae7ae75b2680caffe4c",
    "hidden_file_to_directory": "sha256:997d968b46c23778b079b6123427d7efc69cb97af338ae6bd3e321c01da831d6",
    "too_large": "sha256:bf6f291b376bbbf59756f29e3feffaa4cb3ca1443c03d7e0d01b490251cdcf30",
}


def _entry(
    path: str, data: bytes | None, *, kind: str = "file", executable: bool = False
) -> Entry | None:
    if kind == "absent":
        return None
    return Entry(path, kind, executable, 0 if data is None else len(data))  # type: ignore[arg-type]


def _write(root: Path, path: str, data: bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _fact(
    tmp_path: Path,
    before: bytes | None,
    after: bytes | None,
    *,
    bx: bool = False,
    ax: bool = False,
    hidden: bool = False,
    before_kind: str = "file",
    after_kind: str = "file",
) -> PathChange:
    parent, child = tmp_path / "p", tmp_path / "c"
    parent.mkdir(parents=True, exist_ok=True)
    child.mkdir(parents=True, exist_ok=True)
    path = "skills/a/SKILL.md"
    entries: list[dict[str, Entry]] = []
    for root, data, kind, flag in (
        (parent, before, before_kind, bx),
        (child, after, after_kind, ax),
    ):
        if data is not None:
            _write(root, path, data)
            (root / path).chmod(0o755 if flag else 0o644)
        elif kind == "directory":
            (root / path).mkdir(parents=True, exist_ok=True)
        entry = _entry(
            path,
            data,
            kind="absent" if data is None and kind != "directory" else kind,
            executable=flag,
        )
        entries.append({} if entry is None else {path: entry})
    return path_change(
        parent,
        child,
        path,
        entries[0],
        entries[1],
        visible=(lambda _p: not hidden),
        screen=lambda _line: False,
    )


def test_golden_records_hold_their_digests(tmp_path: Path) -> None:
    edit = _fact(tmp_path / "1", b"a\nold\nz\n", b"a\nnew\nadded\nz\n")
    crlf = _fact(
        tmp_path / "2",
        b"top1\ntop2\ntop3\na\nold\nz\n",
        b"top1\r\ntop2\r\ntop3\r\na\r\nnew\r\nadded\r\nz\r\n",
    )
    assert edit.fingerprint == crlf.fingerprint == GOLDEN["edit_e"]
    assert _fact(tmp_path / "3", None, b"x\ny\n").fingerprint == GOLDEN["created"]
    assert _fact(tmp_path / "4", b"x\ny\n", None).fingerprint == GOLDEN["deleted"]
    assert _fact(tmp_path / "5", b"x\n", b"x\n", ax=True).fingerprint == GOLDEN["exec_only"]
    assert _fact(tmp_path / "6", b"x\r\ny\r\n", b"x\ny\n").fingerprint == GOLDEN["terminators_only"]
    # `b"\x00\x01"` is valid UTF-8, so the binary golden is built from sides directly.
    assert (
        fingerprint(change_record(Side("binary", b"\x00\x01"), Side("binary", b"\x00\x02")))
        == GOLDEN["binary"]
    )
    assert _fact(tmp_path / "7", b"\xff\x00\x01", b"\xff\x00\x02").change["basis"] == "content"  # type: ignore[index]
    assert (
        _fact(tmp_path / "9", None, None, after_kind="directory").fingerprint
        == GOLDEN["directory_created"]
    )
    assert _fact(tmp_path / "10", b"a\n", b"\xff").fingerprint == GOLDEN["text_to_binary"]
    hidden = _fact(tmp_path / "11", b"secret bytes", None, hidden=True, after_kind="directory")
    assert hidden.fingerprint == GOLDEN["hidden_file_to_directory"]
    big = _fact(tmp_path / "12", b"same\n" * 4000, b"same\n" * 1999 + b"other\n" + b"same\n" * 2000)
    assert big.fingerprint == GOLDEN["too_large"]


def test_a_record_names_its_scheme_and_never_its_path(tmp_path: Path) -> None:
    record = change_record(Side("text", b"a\n", ("a\n",)), Side("text", b"b\n", ("b\n",)))
    assert record == {
        "scheme": "reflect_change_fingerprint/1",
        "basis": "lines",
        "before": "text",
        "after": "text",
        "hunks": [[["a\n"], ["b\n"]]],
        "bytes": None,
    }
    assert "path" not in json.dumps(record)


def test_the_same_lines_added_at_another_place_share_a_fingerprint(tmp_path: Path) -> None:
    top = _fact(tmp_path / "1", b"a\nb\nc\n", b"NEW\na\nb\nc\n")
    bottom = _fact(tmp_path / "2", b"a\nb\nc\n", b"a\nb\nc\nNEW\n")
    assert top.fingerprint == bottom.fingerprint  # B1: no positions


def test_side_kinds_and_executable_flags(tmp_path: Path) -> None:
    assert _fact(tmp_path / "1", b"x\n", b"y\n", bx=True, ax=True).change["before"] == "text+x"  # type: ignore[index]
    assert _fact(tmp_path / "2", b"\xff", b"\xfe", ax=True).change["after"] == "binary+x"  # type: ignore[index]


def test_a_hidden_side_contributes_only_its_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real_open = reflect_change.os.open

    def spy_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        opened.append(Path(path).name)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(reflect_change.os, "open", spy_open)
    one = _fact(tmp_path / "1", b"hidden one", None, hidden=True, after_kind="directory")
    two = _fact(
        tmp_path / "2", b"\xffhidden two", None, bx=True, hidden=True, after_kind="directory"
    )
    assert one.fingerprint == two.fingerprint
    assert "SKILL.md" not in opened
    assert one.change == {
        "basis": "content",
        "before": "file",
        "after": "directory",
        "added": None,
        "removed": None,
        "hunks": None,
        "excerpt": None,
        "excerpt_cut": False,
    }


def test_the_work_bound_is_per_side_and_decided_before_the_matcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    real = reflect_change.SequenceMatcher

    def spy(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(reflect_change, "SequenceMatcher", spy)
    started = time.monotonic()
    big = _fact(tmp_path / "1", b"same\n" * 4000, b"same\n" * 1999 + b"other\n" + b"same\n" * 2000)
    assert big.change["basis"] == "too_large" and calls == []  # type: ignore[index]
    assert time.monotonic() - started < 5
    assert _fact(tmp_path / "2", b"x\n" * 1000, b"x\n" * 999 + b"y\n").change["basis"] == "lines"  # type: ignore[index]
    assert (
        _fact(tmp_path / "3", b"x\n" * 1001, b"x\n" * 1000 + b"y\n").change["basis"] == "too_large"
    )  # type: ignore[index]
    wide = b"a" * 65_537
    assert _fact(tmp_path / "4", wide, wide + b"\n").change["basis"] == "too_large"  # type: ignore[index]


def test_a_side_that_no_longer_matches_the_scan_has_no_fingerprint(tmp_path: Path) -> None:
    parent, child = tmp_path / "p", tmp_path / "c"
    _write(parent, "f.md", b"a\n")
    _write(child, "f.md", b"b\n")
    stale = {"f.md": Entry("f.md", "file", False, 99)}
    fact = path_change(
        parent,
        child,
        "f.md",
        stale,
        {"f.md": Entry("f.md", "file", False, 2)},
        visible=None,
        screen=lambda _l: False,
    )
    assert fact == PathChange(None, None)
    gone = path_change(
        parent,
        child,
        "g.md",
        {"g.md": Entry("g.md", "file", False, 1)},
        {},
        visible=None,
        screen=lambda _l: False,
    )
    assert gone == PathChange(None, None)


def test_the_description_counts_lines_and_takes_the_first_non_blank_added_line() -> None:
    record = change_record(
        Side("text", b"", ()),
        Side("text", b"", ("\n", "   \n", "  Always cite the policy.\n", "x\n")),
    )
    assert describe(record, screen=lambda _l: False) == {
        "basis": "lines",
        "before": "text",
        "after": "text",
        "added": 4,
        "removed": 0,
        "hunks": 1,
        "excerpt": "Always cite the policy.",
        "excerpt_cut": False,
    }


def test_the_excerpt_is_cut_by_its_serialized_literal() -> None:
    exact = "\x01" * 19 + "abcd"
    assert len(json.dumps(exact, ensure_ascii=False)) == 120
    assert cut_to_literal(exact, 120) == (exact, False)
    cut, flag = cut_to_literal("\x01" * 20, 120)
    assert flag is True and len(json.dumps(cut, ensure_ascii=False)) <= 120 and cut == "\x01" * 19


def test_the_screen_drops_needles_and_secrets_without_a_trace() -> None:
    declared = "Please call 555-0100 now"
    cases = (
        EvalCase("c1", "g", "s", (), {}, {}, {"answer": declared}),
        EvalCase("c2", "g", "s", (), {}, {}, {"answer": declared + " 45"}),
        EvalCase("c3", "g", "s", (), {}, {}, {"answer": "ORACLE-CANARY-VALUE"}),
    )
    screen = excerpt_screen(oracle_needles(cases, (declared,)), ("sk-RUN-SECRET",))
    plain = change_record(Side("text", b"", ()), Side("text", b"", ("\n",)))
    blank = describe(plain, screen=screen)
    for line, shown in (
        ("say ORACLE-CANARY-VALUE here", False),
        ("token sk-RUN-SECRET", False),
        (f"say {declared}", True),
        (f"say {declared} 45", False),
    ):
        record = change_record(Side("text", b"", ()), Side("text", b"", (line + "\n",)))
        result = describe(record, screen=screen)
        if shown:
            assert result["excerpt"] == line
        else:
            assert {k: result[k] for k in ("excerpt", "excerpt_cut")} == {
                "excerpt": None,
                "excerpt_cut": False,
            }
            assert json.dumps({k: result[k] for k in ("excerpt", "excerpt_cut")}) == json.dumps(
                {k: blank[k] for k in ("excerpt", "excerpt_cut")}
            )


def test_the_screen_never_changes_a_fingerprint(tmp_path: Path) -> None:
    parent, child = tmp_path / "p", tmp_path / "c"
    _write(parent, "f.md", b"a\n")
    _write(child, "f.md", b"a\ntoken sk-RUN-SECRET\n")
    entries = (
        {"f.md": Entry("f.md", "file", False, 2)},
        {"f.md": Entry("f.md", "file", False, 22)},
    )
    open_ = path_change(parent, child, "f.md", *entries, visible=None, screen=lambda _l: False)
    shut = path_change(parent, child, "f.md", *entries, visible=None, screen=lambda _l: True)
    assert open_.fingerprint == shut.fingerprint
    assert open_.change["excerpt"] == "token sk-RUN-SECRET" and shut.change["excerpt"] is None  # type: ignore[index]


def test_the_path_display_rule_is_serialized() -> None:
    ok = "d/" + "x" * 252  # literal 256
    assert path_display(ok) == {"path": ok, "path_sha256": None}
    edge = "d/" + "x" * 254  # literal 258
    assert path_display(edge)["path"] == edge
    over = "d/" + "x" * 255  # literal 259
    shown = path_display(over)
    assert shown["path"] is None and len(str(shown["path_sha256"])) == 64
    escaped = "d/" + "\x01" * 250 + "/f00"
    assert path_display(escaped)["path"] is None


@pytest.mark.parametrize(
    "stale_side", ["symlink", "missing_directory", "changed_absent", "executable"]
)
def test_a_scanned_side_that_changes_kind_or_metadata_has_no_fingerprint(
    tmp_path: Path, stale_side: str
) -> None:
    parent, child = tmp_path / "p", tmp_path / "c"
    parent.mkdir()
    child.mkdir()
    path = "f.md"
    visible = None
    if stale_side == "symlink":
        _write(parent, path, b"old\n")
        _write(child, path, b"new\n")
        outside = tmp_path / "outside"
        outside.write_bytes(b"OUT\n")
        (child / path).unlink()
        (child / path).symlink_to(outside)
        parent_entries = {path: Entry(path, "file", False, 4)}
        child_entries = {path: Entry(path, "file", False, 4)}
    elif stale_side == "missing_directory":
        (child / path).write_bytes(b"x\n")
        parent_entries = {path: Entry(path, "directory", False, 0)}
        child_entries = {path: Entry(path, "file", False, 2)}
    elif stale_side == "changed_absent":
        _write(parent, path, b"appeared\n")
        _write(child, path, b"current\n")
        parent_entries = {}
        child_entries = {path: Entry(path, "file", False, 8)}
    else:
        _write(parent, path, b"x\n")
        _write(child, path, b"y\n")
        (parent / path).chmod(0o755)
        parent_entries = {path: Entry(path, "file", False, 2)}
        child_entries = {path: Entry(path, "file", False, 2)}

    fact = path_change(
        parent,
        child,
        path,
        parent_entries,
        child_entries,
        visible=visible,
        screen=lambda _line: False,
    )
    assert fact == PathChange(None, None)


def test_a_missing_published_root_is_not_treated_as_an_absent_path(
    tmp_path: Path,
) -> None:
    parent, child = tmp_path / "published-parent", tmp_path / "child"
    child.mkdir()
    _write(child, "new.md", b"new\n")

    fact = path_change(
        parent,
        child,
        "new.md",
        {},
        {"new.md": Entry("new.md", "file", False, 4)},
        visible=None,
        screen=lambda _line: False,
    )

    assert fact == PathChange(None, None)


def test_a_new_child_below_a_new_directory_is_still_a_creation(
    tmp_path: Path,
) -> None:
    parent, child = tmp_path / "parent", tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    (parent / "d").write_bytes(b"old file")
    _write(child, "d/f.md", b"new\n")

    fact = path_change(
        parent,
        child,
        "d/f.md",
        {"d": Entry("d", "file", False, 8)},
        {"d/f.md": Entry("d/f.md", "file", False, 4)},
        visible=None,
        screen=lambda _line: False,
    )

    assert fact.fingerprint == _fact(tmp_path / "expected", None, b"new\n").fingerprint
    assert fact.change is not None


def test_an_absent_descendant_below_a_substituted_symlink_is_stale(
    tmp_path: Path,
) -> None:
    parent, child = tmp_path / "parent", tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.md").write_bytes(b"outside\n")
    (parent / "d").symlink_to(outside, target_is_directory=True)
    _write(child, "d/f.md", b"new\n")

    fact = path_change(
        parent,
        child,
        "d/f.md",
        {},
        {"d/f.md": Entry("d/f.md", "file", False, 4)},
        visible=None,
        screen=lambda _line: False,
    )

    assert fact == PathChange(None, None)


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"),
    reason="FIFO substitution requires POSIX open flags",
)
def test_a_fifo_substituted_after_stat_is_rejected_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, child = tmp_path / "p", tmp_path / "c"
    _write(parent, "f.md", b"old\n")
    _write(child, "f.md", b"new\n")
    entries = {"f.md": Entry("f.md", "file", False, 4)}
    real_open = reflect_change.os.open
    final_opens = 0

    def swap_to_fifo(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal final_opens
        if path == "f.md":
            final_opens += 1
            if final_opens == 2:
                target = child / "f.md"
                target.unlink()
                os.mkfifo(target)
                assert flags & os.O_NONBLOCK
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(reflect_change.os, "open", swap_to_fifo)
    assert path_change(
        parent,
        child,
        "f.md",
        entries,
        entries,
        visible=None,
        screen=lambda _line: False,
    ) == PathChange(None, None)


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
def test_history_excerpt_check_modes(mode):
    from evolution.tree.execution.check_modes import HitSink
    from evolution.tree.roles.reflector.reflect_change import excerpt_screen

    hits = HitSink()
    blocked = excerpt_screen(("oracle-123456",), ("secret-123456",), mode=mode, hits=hits)
    assert blocked("oracle-123456") is (mode == "enforce")
    assert blocked("secret-123456") is (mode == "enforce")
    assert not blocked("ordinary text")
    assert hits.entries() == (
        [{"check": "history_excerpt_screen", "code": "excerpt_dropped", "count": 2}]
        if mode == "shadow"
        else []
    )
