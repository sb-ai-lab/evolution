import json
import os

import pytest

from evolution import __version__
from evolution.tree.artifacts.artifact_errors import ArtifactUnsafeError
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifacts import ArtifactStore
from evolution.tree.execution.candidate_integrity import ArtifactError
from tests.tree.artifacts.test_artifact_policy import seed, write

POLICY = ArtifactPolicyV1(("private/**",), ("uv.lock",), ("skills/**",))
VR = lambda _: __version__  # noqa: E731
CANARY_EXCLUDED = b"CANARY-EXCLUDED-7f3a"
CANARY_ENV = b"CANARY-ENV-9c1d"
CANARY_EDITOR = b"CANARY-EDITOR-2b8e"
CANARY_ARTIFACT = b"CANARY-ARTIFACT-5e4f"


def everything(run):
    blobs = [p.read_bytes() for p in run.rglob("*") if p.is_file()]
    return b"\n".join(blobs)


def test_canaries_never_reach_published_copies_ledger_or_projection(tmp_path):
    src = seed(tmp_path)
    write(src, "private/keys.txt", CANARY_EXCLUDED)
    write(src, ".env", CANARY_ENV)
    write(src, "skills/SKILL.md", CANARY_ARTIFACT)
    run = tmp_path / "run"
    with ArtifactStore.create(
        run,
        src,
        POLICY,
        parallel_attempts=1,
        version_resolver=VR,
        secrets=(CANARY_ARTIFACT.decode(),),
    ) as store:
        ws = store.materialize_candidate("a1")
        write(ws, ".env", CANARY_EDITOR)
        write(ws, "skills/new/SKILL.md", b"fine")
        store.seal("a1")
        published = everything(run / "artifacts")
        assert CANARY_EXCLUDED not in published
        assert CANARY_ENV not in published and CANARY_EDITOR not in published
        assert CANARY_ARTIFACT in published  # artifact bytes are copied by design
        ledger = (run / "artifacts.jsonl").read_bytes()
        for canary in (CANARY_EXCLUDED, CANARY_ENV, CANARY_EDITOR, CANARY_ARTIFACT):
            assert canary not in ledger
        assert str(src) not in ledger.decode()
        view = json.dumps(ArtifactStore.inspect(run), default=lambda o: o.__dict__)
        assert "CANARY" not in view and str(src) not in view
        verified = json.dumps(ArtifactStore.inspect(run, verify=True), default=lambda o: o.__dict__)
        assert "CANARY" not in verified and str(src) not in verified


def test_canaries_never_reach_a_raised_diagnostic(tmp_path):
    src = seed(tmp_path)
    write(src, "skills/SKILL.md", CANARY_ARTIFACT)
    run = tmp_path / "run"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(CANARY_EXCLUDED)
    with ArtifactStore.create(run, src, POLICY, parallel_attempts=1, version_resolver=VR) as store:
        ws = store.materialize_candidate("a1")
        (ws / "skills/alias.md").hardlink_to(outside)
        with pytest.raises(ArtifactUnsafeError) as info:
            store.seal("a1")
    assert info.value.code == "hard_link" and str(info.value) == "hard_link"
    assert "CANARY" not in str(info.value) and str(src) not in str(info.value)
    assert outside.read_bytes() == CANARY_EXCLUDED
    assert not (run / "artifacts/candidates/a1").exists()


def test_an_unreadable_seed_never_names_the_seed_path(tmp_path):
    """The untranslated scan raised a raw `PermissionError` whose message carries
    the operator's absolute seed path; the typed boundary carries only the code.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file, so the failure cannot be provoked")
    src = seed(tmp_path)
    write(src, "skills/SKILL.md", CANARY_ARTIFACT)
    (src / "skills/SKILL.md").chmod(0o000)
    try:
        with pytest.raises(ArtifactError) as info:
            ArtifactStore.create(
                tmp_path / "run", src, POLICY, parallel_attempts=1, version_resolver=VR
            )
    finally:
        (src / "skills/SKILL.md").chmod(0o644)
    assert info.value.code == "io_failure" and str(info.value) == "io_failure"
    assert str(src) not in str(info.value) and "CANARY" not in str(info.value)
    assert not (tmp_path / "run").exists()


def test_store_writes_only_its_owned_entries(tmp_path):
    src = seed(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    (run / "measurements").mkdir()
    sentinel = run / "measurements/keep.txt"
    sentinel.write_bytes(b"c1 owns this")
    with ArtifactStore.create(run, src, POLICY, parallel_attempts=1, version_resolver=VR) as store:
        store.materialize_candidate("a1")
        store.abandon("a1")
    assert sorted(p.name for p in run.iterdir()) == [
        ".artifacts.lock",
        "artifacts",
        "artifacts.jsonl",
        "measurements",
    ]
    assert sentinel.read_bytes() == b"c1 owns this"
    assert sorted(p.name for p in (run / "artifacts").iterdir()) == [
        ".staging",
        ".workspaces",
        "candidates",
        "champions",
    ]
