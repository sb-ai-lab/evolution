"""Shared strict-loader contracts exercised with native `/15` documents."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import MappingProxyType

import pytest

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.execution.config import ManifestError
from evolution.tree.manifest.manifest_v2 import (
    SCHEMA_V15,
    ManifestV2,
    _positive_number_to_float,
    load_manifest_v2,
)
from evolution.tree.roles.config import ChatEditorSection
from evolution.tree.roles.role_call import RoleCallRetry
from evolution.tree.target.config import TargetBinding, Timeouts
from tests.scaffold_v13 import SAMPLE_MANIFEST_V13


def manifest_payload_example() -> dict:
    """Mirrors the contract's example JSON verbatim, for reuse by other tests."""
    return {
        "schema": "evolution-v2-manifest/1",
        "seed_root": "./agent",
        "target": {
            "base_url": "http://127.0.0.1:8765",
            "seed_mode": "exact",
            "contract_digest": None,
            "settings_hash": None,
            "max_response_bytes": 67108864,
            "min_input_bytes": 1048576,
            "timeouts": {"connect_seconds": 5.0, "read_seconds": 120.0},
        },
        "artifacts": {
            "excludes": ["build/**"],
            "dependency_paths": ["uv.lock"],
            "writable_paths": ["skills/**"],
        },
    }


def write_manifest(tmp_path: Path, payload: dict, seed_dir: str = "agent") -> Path:
    (tmp_path / seed_dir).mkdir(parents=True, exist_ok=True)
    (tmp_path / "uv.lock").write_text("locked\n")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload))
    return manifest_path


def write_v5_document(tmp_path: Path, document: dict) -> Path:
    """Write any manifest document verbatim; only `seed_root` need exist on
    disk for the loader (the module docstring: every other workflow path is
    resolved lexically, never checked for existence, by `/2` onward). The
    sample scaffold's own document is `/7` (W19: `/6` plus a required
    `campaign.budget.max_failed_turns`); callers that want `/5` specifically
    set `document["schema"]`."""

    (tmp_path / "seed").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document))
    return path


def _pre_v7_document() -> dict:
    """The frozen `/13` scaffold sample (`tests/scaffold_v13.py`) with
    `campaign.call_retries`, `campaign.budget.max_failed_turns`,
    `reflector.max_consecutive_failures` and the `/13` gate keys stripped, so
    it fits `/5` and `/6` (which forbid all of them as `unknown_key`) without
    every caller repeating the strip. `v7_document` adds the budget key back
    and `v8_document` the retries, each for its own cases. The sample is
    frozen (H12c choice 7) because the scaffold writes `/15` from
    `2.0.0b1`."""

    document = copy.deepcopy(SAMPLE_MANIFEST_V13)
    document["campaign"]["budget"].pop("max_failed_turns", None)
    document["campaign"].pop("call_retries", None)
    document["reflector"].pop("max_consecutive_failures", None)
    # The `/13` gate keys are `unknown_key` on every older schema.
    for key in ("provenance", "fresh_parent", "global"):
        document["campaign"]["gate"].pop(key, None)
    return document


# -- `evolution-v2-manifest/7`: `/6` plus a required `campaign.budget.max_failed_turns` -


def v7_document(max_failed_turns: object = 3) -> dict:
    """The frozen `/13` scaffold sample (`tests/scaffold_v13.py`), reduced to
    a `/7` document, its `max_failed_turns` overridden (or stripped) for the
    case under test.

    `SAMPLE_MANIFEST_V13` declares `max_failed_turns`, and `_pre_v7_document`
    removes it; `max_failed_turns=None` leaves it removed, which is what makes
    a `/7` document incomplete and a `/6` one valid.
    """

    document = _pre_v7_document()
    if max_failed_turns is not None:
        document["campaign"]["budget"]["max_failed_turns"] = max_failed_turns
    return document


# -- `evolution-v2-manifest/8`: `/7` plus a required `campaign.call_retries` (I1) -


_V8_CALL_RETRIES = {"max_attempts": 4, "turn_max_attempts": 1}


def v8_document(call_retries: object = _V8_CALL_RETRIES) -> dict:
    """The scaffold's own sample document as `/7` plus `campaign.call_retries`
    (or, for `call_retries=None`, without it -- the incomplete `/8` case)."""

    document = v7_document(3)
    if call_retries is not None:
        document["campaign"]["call_retries"] = call_retries
    return document


MODEL_GROUPING = {
    "kind": "model",
    "backend": "openai_chat",
    "model": "grouper-small",
    "base_url": "https://llm.example.test/v1",
    "api_key_env": "GROUPER_KEY",
    "timeout_seconds": 600,
    "concurrency": 4,
    "max_input_chars": 200000,
    "max_output_tokens": 8192,
    "retry": {
        "version": "role_call_retry/1",
        "max_attempts": 3,
        "total_budget_seconds": 90,
        "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 15, "jitter": "full"},
    },
}


# -- `evolution-v2-manifest/11`: `/10` plus `reflector.max_consecutive_failures`
# and a required `retry` on chat Reflector and chat Editor (PDR-0019, ADR-0023) -


def v11_document(
    max_consecutive_failures: object = 2, call_retries: object = _V8_CALL_RETRIES
) -> dict:
    """The scaffold's own sample document (subprocess roles) as `/8` plus
    `reflector.max_consecutive_failures` (or, for `max_consecutive_failures=None`,
    without it -- the incomplete `/11` case)."""

    document = v8_document(call_retries)
    if max_consecutive_failures is not None:
        document["reflector"]["max_consecutive_failures"] = max_consecutive_failures
    return document


CHAT_REFLECTOR = {
    "kind": "chat",
    "model": "gpt-5.5",
    "base_url": "https://reflector.example.com/v1",
    "api_key_env": "EVO_REFLECTOR_API_KEY",
}

RETRY = {
    "version": "role_call_retry/1",
    "max_attempts": 4,
    "total_budget_seconds": 120,
    "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 20, "jitter": "full"},
}


def v11_chat_document(*, editor_retry: object = None, reflector_retry: object = None) -> dict:
    """`v11_document` with `editor.kind`/`reflector.kind` both `chat`: the
    fixture chat Reflector/Editor manifest tests need (no existing document
    builder combines both, so it is derived here from `v11_document`'s
    subprocess base and the file's own `CHAT` editor payload)."""

    document = v11_document()
    document["editor"] = dict(CHAT)
    if editor_retry is not None:
        document["editor"]["retry"] = editor_retry
    document["reflector"] = {
        **CHAT_REFLECTOR,
        "environment_docs": None,
        "probe": None,
        "grouping": document["reflector"]["grouping"],
        "selection": document["reflector"]["selection"],
        "families": document["reflector"]["families"],
        "max_consecutive_failures": document["reflector"]["max_consecutive_failures"],
    }
    if reflector_retry is not None:
        document["reflector"]["retry"] = reflector_retry
    return document


# -- `editor.kind`: `chat` and `agent_cli` beside `subprocess` (rule R1) -----


CHAT = {
    "kind": "chat",
    "model": "gpt-5.5",
    "base_url": "https://api.example.com/v1",
    "api_key_env": "EVO_EDITOR_API_KEY",
}

AGENT = {
    "kind": "agent_cli",
    "model": "codex",
}


# -- `_declared_spellings` fills `ManifestV2.declared` for parsed sections -


def test_missing_manifest_file_is_bad_path(tmp_path):
    """`detail` is a key path here too, never the path that could not be read.

    Callers print it: `evo tree conformance` echoes `code: detail` to stderr and
    `evo tree evolve` carries it as the public `path` of its refusal, so a
    resolved filesystem path here would be one in a refusal.
    """

    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(tmp_path / "does-not-exist.json")
    assert exc_info.value.code == "bad_path"
    assert exc_info.value.detail == "manifest"
    assert str(exc_info.value) == "bad_path: manifest"
    assert str(tmp_path) not in str(exc_info.value)


def test_an_unreadable_manifest_names_no_path_either(tmp_path):
    """A directory, and a file that is not UTF-8, take the same `bad_path` shape."""

    directory = tmp_path / "manifest.json"
    directory.mkdir()
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(directory)
    assert (exc_info.value.code, exc_info.value.detail) == ("bad_path", "manifest")
    assert str(tmp_path) not in str(exc_info.value)

    not_utf8 = tmp_path / "binary.json"
    not_utf8.write_bytes(b"\xff\xfe{}")
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(not_utf8)
    assert (exc_info.value.code, exc_info.value.detail) == ("bad_path", "manifest")
    assert str(tmp_path) not in str(exc_info.value)


def test_invalid_json_is_bad_type(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{not json")
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(manifest_path)
    assert exc_info.value.code == "bad_type"


def test_non_object_root_is_bad_type(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(manifest_path)
    assert exc_info.value.code == "bad_type"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com:80",
        "https://127.0.0.1:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:8765/skills",
    ],
)
def test_the_binding_itself_refuses_a_non_loopback_base_url(base_url):
    """The loopback rule is an invariant of the value, not of the document: a
    binding built in Python is as unable to name a remote host as a loaded one,
    which is what lets `HttpTransport` split `base_url` without re-deciding."""
    with pytest.raises(ValueError):
        TargetBinding(
            base_url=base_url, seed_mode="exact", contract_digest=None, settings_hash=None
        )


def test_the_binding_normalizes_its_base_url():
    binding = TargetBinding(
        base_url="http://localhost:8765/",
        seed_mode="exact",
        contract_digest=None,
        settings_hash=None,
    )

    assert binding.base_url == "http://localhost:8765"
    assert (
        TargetBinding(
            base_url="http://[::1]:8765", seed_mode="none", contract_digest=None, settings_hash=None
        ).base_url
        == "http://[::1]:8765"
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_is_bad_type_at_the_field(value):
    """The decoder already refuses the three JSON tokens; the per-field guard is
    what makes a non-finite number impossible however the value arrived."""
    with pytest.raises(ManifestError) as exc_info:
        _positive_number_to_float(value, "target.timeouts.read_seconds")
    assert exc_info.value.code == "bad_type"
    assert exc_info.value.detail == "target.timeouts.read_seconds"


def test_manifest_error_str_is_code_colon_detail():
    err = ManifestError("bad_type", "target.timeouts.read_seconds")
    assert str(err) == "bad_type: target.timeouts.read_seconds"


def test_chat_digest_unchanged_without_retry():
    from evolution.tree.roles.seals import section_payload

    section = ChatEditorSection(
        model="gpt-5.5", base_url="https://api.example.com/v1", api_key_env="EVO_EDITOR_API_KEY"
    )
    assert "retry" not in section_payload(section)


def test_chat_digest_seals_retry_as_its_canonical_payload():
    """A `retry` present in the digest is `RoleCallRetry.to_payload()`'s
    canonical `role_call_retry/1` shape (nested `backoff`), never
    `dataclasses.asdict`'s flat rendering of the dataclass's own fields (fix
    round 1: this branch of `section_payload` had no direct test)."""

    import dataclasses

    from evolution.tree.roles.seals import section_payload

    retry = RoleCallRetry.from_payload(RETRY)
    section = ChatEditorSection(
        model="gpt-5.5",
        base_url="https://api.example.com/v1",
        api_key_env="EVO_EDITOR_API_KEY",
        retry=retry,
    )
    payload = section_payload(section)
    assert payload["retry"] == retry.to_payload()
    assert payload["retry"] != dataclasses.asdict(retry)


def test_manifest_declared_defaults_to_an_empty_read_only_mapping(tmp_path):
    """Every existing constructor call keeps working; the field is frozen all the way down."""
    manifest = ManifestV2(
        schema=SCHEMA_V15,
        path=tmp_path / "manifest.json",
        seed_root=tmp_path / "seed",
        target=TargetBinding("http://127.0.0.1:8765", "exact", None, None),
        policy=ArtifactPolicyV1((), (), ()),
    )
    assert isinstance(manifest.declared, MappingProxyType)
    assert dict(manifest.declared) == {}
    with pytest.raises(TypeError):
        manifest.declared["seed_root"] = "seed"  # type: ignore[index]


def test_manifest_declared_is_a_private_copy_of_the_constructor_argument(tmp_path):
    source: dict[str, str | None] = {"seed_root": "seed", "editor.system_prompt": None}
    manifest = ManifestV2(
        schema=SCHEMA_V15,
        path=tmp_path / "manifest.json",
        seed_root=tmp_path / "seed",
        target=TargetBinding("http://127.0.0.1:8765", "exact", None, None),
        policy=ArtifactPolicyV1((), (), ()),
        declared=source,
    )
    source["seed_root"] = "elsewhere"
    source["corpus.path"] = "x.jsonl"
    assert dict(manifest.declared) == {"seed_root": "seed", "editor.system_prompt": None}


def shared_payload():
    """Native /15 vehicle for the original shared target/artifact assertions."""
    from tests.tree.manifest.test_manifest_v15 import v15_from

    document = v15_from(v11_document())
    document.update(manifest_payload_example())
    document["schema"] = SCHEMA_V15
    return document


def shared_workflow(tmp_path, **overrides):
    """Write a native document with the original shared path spellings."""
    from tests.tree.manifest.test_manifest_v15 import v15_from

    document = v15_from(v11_document())
    document["seed_root"] = "seed"
    document["corpus"] = {"path": "corpus.jsonl"}
    for name in ("scorer", "editor", "reflector"):
        section = document[name]
        section.update(argv=["python3", "main.py"], cwd=name, dependency_paths=[], allowed_env=[])
    document["scorer"]["timeout_seconds"] = 60
    document["editor"].pop("timeout_seconds", None)
    document["reflector"]["environment_docs"] = None
    document["reflector"]["probe"] = None
    document.update(overrides)
    return write_manifest(tmp_path, document, seed_dir="seed")


def shared_chat_manifest(tmp_path, editor):
    """Supply /15's explicit role retry and compaction before field mutations."""
    if editor.get("kind") == "chat":
        editor = {
            "retry": RETRY,
            "context_compact_chars": 128_000,
            "context_message_max_chars": 12_000,
            **editor,
        }
    return shared_workflow(tmp_path, editor=editor)


def test_defaults_applied_when_optional_fields_absent(tmp_path):
    payload = shared_payload()
    for key in (
        "contract_digest",
        "settings_hash",
        "max_response_bytes",
        "min_input_bytes",
        "timeouts",
    ):
        del payload["target"][key]
    manifest = load_manifest_v2(write_manifest(tmp_path, payload))
    assert manifest.target.contract_digest is None
    assert manifest.target.settings_hash is None
    assert manifest.target.max_response_bytes == 67108864
    assert manifest.target.min_input_bytes == 1048576
    assert manifest.target.timeouts == Timeouts()


def test_pinned_digests_and_int_timeouts_are_accepted(tmp_path):
    payload = shared_payload()
    payload["target"]["contract_digest"] = "contract-1"
    payload["target"]["settings_hash"] = "settings-1"
    payload["target"]["timeouts"] = {"connect_seconds": 3, "read_seconds": 60}
    manifest = load_manifest_v2(write_manifest(tmp_path, payload))
    assert manifest.target.contract_digest == "contract-1"
    assert manifest.target.settings_hash == "settings-1"
    assert manifest.target.timeouts == Timeouts(connect_seconds=3.0, read_seconds=60.0)
    assert isinstance(manifest.target.timeouts.connect_seconds, float)
    assert isinstance(manifest.target.timeouts.read_seconds, float)


def test_base_url_normalizes_trailing_slash(tmp_path):
    payload = shared_payload()
    payload["target"]["base_url"] = "http://localhost:9000/"
    manifest = load_manifest_v2(write_manifest(tmp_path, payload))
    assert manifest.target.base_url == "http://localhost:9000"


@pytest.mark.parametrize(
    ("edit", "code", "needle"),
    [
        (lambda p: p.update(extra="nope"), "unknown_key", "extra"),
        (lambda p: p["target"].update(bogus=True), "unknown_key", "target.bogus"),
        (lambda p: p["artifacts"].update(bogus=[]), "unknown_key", "artifacts.bogus"),
        (lambda p: p["target"]["timeouts"].update(bogus=1), "unknown_key", "target.timeouts.bogus"),
        (lambda p: p.pop("artifacts"), "missing_key", "artifacts"),
        (lambda p: p["target"].pop("base_url"), "missing_key", "target.base_url"),
        (
            lambda p: p["target"]["timeouts"].update(read_seconds="x"),
            "bad_type",
            "target.timeouts.read_seconds",
        ),
        (
            lambda p: p["target"].update(base_url=["http://127.0.0.1:8765"]),
            "bad_type",
            "target.base_url",
        ),
    ],
)
def test_shared_field_refusals(tmp_path, edit, code, needle):
    payload = shared_payload()
    edit(payload)
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(write_manifest(tmp_path, payload))
    assert exc_info.value.code == code
    assert needle in str(exc_info.value)


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p.pop("target"),
        lambda p: p.pop("seed_root"),
    ],
)
def test_shared_required_sections(tmp_path, edit):
    payload = shared_payload()
    edit(payload)
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(write_manifest(tmp_path, payload))
    assert exc_info.value.code == "missing_key"


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p["target"]["timeouts"].update(connect_seconds=True),
        lambda p: p["target"].update(max_response_bytes=True),
        lambda p: p["target"].update(min_input_bytes=0),
        lambda p: p["target"].update(seed_mode="sometimes"),
        lambda p: p["target"].update(contract_digest=""),
        lambda p: p["target"].update(contract_digest="has a space"),
        lambda p: p["artifacts"].update(excludes="build/**"),
        lambda p: p["artifacts"].update(excludes=["build/**", 5]),
        lambda p: p.update(target="not-an-object"),
        lambda p: p.update(artifacts=["nope"]),
        lambda p: p["target"].update(timeouts="nope"),
        lambda p: p.update(seed_root=5),
        lambda p: p["artifacts"].update(excludes=["../escape"]),
    ],
)
def test_shared_bad_types(tmp_path, edit):
    payload = shared_payload()
    edit(payload)
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(write_manifest(tmp_path, payload))
    assert exc_info.value.code == "bad_type"


@pytest.mark.parametrize("seed_mode", [["exact"], {"mode": "exact"}, 1])
def test_seed_mode_unhashable_or_non_string_is_bad_type_not_a_crash(tmp_path, seed_mode):
    payload = shared_payload()
    payload["target"]["seed_mode"] = seed_mode
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(write_manifest(tmp_path, payload))
    assert exc_info.value.code == "bad_type"
    assert "target.seed_mode" in str(exc_info.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://10.0.0.5:1",
        "https://127.0.0.1:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:8765/skills",
        "http://127.0.0.1:8765/?x=1",
        "http://127.0.0.1:8765#frag",
        "http://user:pass@127.0.0.1:8765",
    ],
)
def test_non_loopback_variants_are_rejected(tmp_path, base_url):
    payload = shared_payload()
    payload["target"]["base_url"] = base_url
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(write_manifest(tmp_path, payload))
    assert exc_info.value.code == "not_loopback"


@pytest.mark.parametrize("seed_root", ["./nowhere", "./uv.lock"])
def test_invalid_seed_root_is_bad_path(tmp_path, seed_root):
    payload = shared_payload()
    payload["seed_root"] = seed_root
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(write_manifest(tmp_path, payload))
    assert exc_info.value.code == "bad_path"


def test_symlinked_seed_root_is_bad_path(tmp_path):
    payload = shared_payload()
    real_dir = tmp_path / "real-agent"
    real_dir.mkdir()
    (tmp_path / "agent-link").symlink_to(real_dir, target_is_directory=True)
    payload["seed_root"] = "./agent-link"
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(write_manifest(tmp_path, payload))
    assert exc_info.value.code == "bad_path"


def test_absolute_seed_root_is_accepted(tmp_path):
    payload = shared_payload()
    seed_dir = tmp_path / "elsewhere"
    seed_dir.mkdir()
    payload["seed_root"] = str(seed_dir)
    manifest = load_manifest_v2(write_manifest(tmp_path, payload))
    assert manifest.seed_root == seed_dir.resolve()


@pytest.mark.parametrize(
    ("token", "value"),
    [("NaN", float("nan")), ("Infinity", float("inf")), ("-Infinity", float("-inf"))],
)
def test_non_finite_json_constants_are_bad_type(tmp_path, token, value):
    payload = shared_payload()
    payload["target"]["timeouts"]["connect_seconds"] = value
    manifest_path = write_manifest(tmp_path, payload)
    assert token in manifest_path.read_text(encoding="utf-8")
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(manifest_path)
    assert exc_info.value.code == "bad_type"
    assert exc_info.value.detail == "$"


def test_manifest_error_detail_never_carries_file_contents(tmp_path):
    payload = shared_payload()
    payload["target"]["timeouts"]["read_seconds"] = "a very secret value that must not leak"
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(write_manifest(tmp_path, payload))
    assert "secret" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (lambda p: p["target"].update(max_response_bytes="x"), "artifacts.excludes"),
        (lambda p: p["target"].update(base_url="http://10.0.0.5:1"), "artifacts.excludes"),
        (lambda p: p["target"].update(bogus=True), "artifacts.excludes"),
        (lambda p: p.update(extra="nope"), "target.base_url"),
    ],
)
def test_shared_structure_precedes_field_errors(tmp_path, edit, needle):
    payload = shared_payload()
    edit(payload)
    branch, key = needle.split(".")
    del payload[branch][key]
    with pytest.raises(ManifestError) as exc_info:
        load_manifest_v2(write_manifest(tmp_path, payload))
    assert exc_info.value.code == "missing_key"
    assert needle in str(exc_info.value)


def test_shared_key_field_defect_outranks_a_defect_inside_a_present_workflow_section(tmp_path):
    path = shared_workflow(tmp_path)
    doc = json.loads(path.read_text())
    doc["target"]["seed_mode"] = 3
    del doc["scorer"]["argv"]
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as info:
        load_manifest_v2(path)
    assert (info.value.code, info.value.detail) == ("bad_type", "target.seed_mode")


def test_top_level_missing_workflow_section_outranks_a_shared_key_field_defect(tmp_path):
    path = shared_workflow(tmp_path)
    doc = json.loads(path.read_text())
    doc["target"]["seed_mode"] = 3
    del doc["corpus"]
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as info:
        load_manifest_v2(path)
    assert (info.value.code, info.value.detail) == ("missing_key", "corpus")


def test_a_symlinked_manifest_resolves_every_relative_path_against_the_real_file(tmp_path):
    """The launcher's decoy seed must never disagree with the workflow base."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    real = shared_workflow(bundle)
    launch = tmp_path / "launch"
    (launch / "seed" / "skills").mkdir(parents=True)
    link = launch / "manifest.json"
    link.symlink_to(real)
    manifest = load_manifest_v2(link)
    assert manifest.seed_root == (bundle / "seed").resolve()
    assert manifest.workflow is not None
    assert manifest.workflow.scorer.cwd == (bundle / "scorer").resolve()
    assert manifest.workflow.corpus_path == (bundle / "corpus.jsonl").resolve()


@pytest.mark.parametrize(
    "base_url",
    [
        "https:",
        "https://",
        "https://example.com:notaport/v1",
        "https://example.com:99999/v1",
        "https://@example.com/v1",
        "https://has space.example/v1",
        "https://example.com\x00bad/v1",
        "https://exa_mple.com/v1",
        "https://-bad.example/v1",
        "https://example..com/v1",
        "https://[::1/v1",
        # plain `http` to any host keeps every other rule.
        "http:",
        "http://",
        "http://u:p@llm.internal:8000/v1",
        "http://@llm.internal/v1",
        "http://llm.internal:8000/v1?x=1",
        "http://llm.internal:8000/v1#part",
        "http://llm.internal:notaport/v1",
        "http://llm.internal:99999/v1",
        "http://llm internal/v1",
        "http://llm.internal/v1\x07",
        "http://exa_mple.internal/v1",
        "ftp://llm.internal/v1",
        "ws://llm.internal:8000/v1",
    ],
)
def test_a_malformed_chat_base_url_is_refused_at_load(tmp_path, base_url):
    manifest = shared_chat_manifest(tmp_path, {**CHAT, "base_url": base_url})
    with pytest.raises(ManifestError) as info:
        load_manifest_v2(manifest)
    assert (info.value.code, info.value.detail) == ("bad_value", "editor.base_url")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.com/v1",
        "https://example.com:8443/v1",
        "http://127.0.0.1:8080/v1",
        "https://api.example-gw.io/v1",
        "https://[::1]:8443/v1",
        "http://localhost:8080/v1",
        "http://127.0.0.1/v1",
        "https://192.0.2.10/v1",
        # plain `http` to a non-loopback host, by name, IPv4 and IPv6.
        "http://llm.internal:8000/v1",
        "http://10.0.0.5:8080/v1",
        "http://[fd00::1]:8000/v1",
    ],
)
def test_a_well_formed_chat_base_url_is_kept_verbatim(tmp_path, base_url):
    loaded = load_manifest_v2(shared_chat_manifest(tmp_path, {**CHAT, "base_url": base_url}))
    assert loaded.workflow is not None
    editor = loaded.workflow.editor
    assert isinstance(editor, ChatEditorSection)
    assert editor.base_url == base_url


@pytest.mark.parametrize(("effort", "code"), [(3, "bad_type"), ("ultra", "bad_value")])
def test_reasoning_effort_refusals(tmp_path, effort, code):
    with pytest.raises(ManifestError) as info:
        load_manifest_v2(shared_chat_manifest(tmp_path, {**CHAT, "reasoning_effort": effort}))
    assert (info.value.code, info.value.detail) == (code, "editor.reasoning_effort")


@pytest.mark.parametrize(
    ("edit", "code", "detail"),
    [
        (lambda r: r.update(extra=1), "unknown_key", "editor.retry.extra"),
        (
            lambda r: r.pop("total_budget_seconds"),
            "missing_key",
            "editor.retry.total_budget_seconds",
        ),
        (lambda r: r["backoff"].update(extra=1), "bad_value", "editor.retry.backoff"),
        (lambda r: r.update(max_attempts=0), "bad_value", "editor.retry.max_attempts"),
    ],
)
def test_role_retry_error_mapping(tmp_path, edit, code, detail):
    retry = copy.deepcopy(RETRY)
    edit(retry)
    with pytest.raises(ManifestError) as info:
        load_manifest_v2(shared_chat_manifest(tmp_path, {**CHAT, "retry": retry}))
    assert (info.value.code, info.value.detail) == (code, detail)


@pytest.mark.parametrize("entry", ["missing-dir", "file", "docs-link"])
def test_environment_docs_bad_path(tmp_path, entry):
    path = shared_workflow(tmp_path)
    (tmp_path / "file").write_text("GET /api\n")
    (tmp_path / "real-docs").mkdir()
    (tmp_path / "docs-link").symlink_to(tmp_path / "real-docs", target_is_directory=True)
    doc = json.loads(path.read_text())
    doc["reflector"]["environment_docs"] = entry
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError, match="bad_path: reflector.environment_docs"):
        load_manifest_v2(path)


def test_environment_docs_must_be_a_directory_outside_the_seed(tmp_path):
    path = shared_workflow(tmp_path)
    (tmp_path / "docs").mkdir()
    doc = json.loads(path.read_text())
    doc["reflector"]["environment_docs"] = "docs"
    path.write_text(json.dumps(doc))
    section = load_manifest_v2(path).workflow.reflector
    assert section.environment_docs == (tmp_path / "docs").resolve()
    doc["reflector"]["environment_docs"] = "seed"
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError, match="program_inside_seed: reflector.environment_docs"):
        load_manifest_v2(path)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("scorer", "cwd"),
        ("editor", "cwd"),
        ("scorer", "dependency_paths[0]"),
        ("editor", "argv[1]"),
    ],
)
def test_program_inside_seed_is_refused_at_load(tmp_path, section, field):
    path = shared_workflow(tmp_path)
    doc = json.loads(path.read_text())
    seed_dir = tmp_path / "seed"
    (seed_dir / "skills").mkdir()
    seed_file = seed_dir / "skills" / "SKILL.md"
    seed_file.write_text("line\n")
    (seed_dir / "uv.lock").write_text("x\n")
    if field == "cwd":
        doc[section]["cwd"] = str(seed_file.parent)
    elif field == "dependency_paths[0]":
        doc[section]["dependency_paths"] = [str((seed_dir / "uv.lock").resolve())]
    elif field == "argv[1]":
        doc[section]["argv"] = [doc[section]["argv"][0], str(seed_file)]
    else:
        raise AssertionError(f"unhandled field {field!r}")
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as info:
        load_manifest_v2(path)
    assert (info.value.code, info.value.detail) == ("program_inside_seed", f"{section}.{field}")


@pytest.mark.parametrize(
    ("editor", "code", "path"),
    [
        ({"kind": "chat"}, "missing_key", "editor"),
        ({"kind": 3}, "bad_type", "editor.kind"),
        ({"kind": "gui"}, "bad_value", "editor.kind"),
        ({**CHAT, "argv": []}, "unknown_key", "editor.argv"),
        ({**CHAT, "base_url": "ftp://example.com/v1"}, "bad_value", "editor.base_url"),
        ({**CHAT, "base_url": "https://u:p@h/v1"}, "bad_value", "editor.base_url"),
        ({**CHAT, "api_key_env": "WITH=EQUALS"}, "bad_value", "editor.api_key_env"),
        ({**CHAT, "api_key_env": " "}, "bad_value", "editor.api_key_env"),
        ({**CHAT, "api_key_env": ""}, "bad_value", "editor.api_key_env"),
        ({**CHAT, "api_key_env": 1}, "bad_type", "editor.api_key_env"),
        ({**CHAT, "reasoning_effort": "ultra"}, "bad_value", "editor.reasoning_effort"),
        ({**CHAT, "quantizations": ["fp8"]}, "bad_value", "editor.quantizations"),
        ({**CHAT, "max_turns": True}, "bad_type", "editor.max_turns"),
        ({**CHAT, "max_turns": 0}, "bad_value", "editor.max_turns"),
        ({**CHAT, "context_compact_chars": "400000"}, "bad_type", "editor.context_compact_chars"),
        ({**CHAT, "context_compact_chars": 0}, "bad_value", "editor.context_compact_chars"),
        (
            {**CHAT, "context_message_max_chars": True},
            "bad_type",
            "editor.context_message_max_chars",
        ),
        (
            {**CHAT, "context_message_max_chars": 200_000},
            "bad_value",
            "editor.context_message_max_chars",
        ),
        (
            {**CHAT, "context_compact_chars": 10_000},
            "bad_value",
            "editor.context_message_max_chars",
        ),
        (
            {**AGENT, "context_compact_chars": 400_000},
            "unknown_key",
            "editor.context_compact_chars",
        ),
        ({**CHAT, "system_prompt": "missing.md"}, "bad_path", "editor.system_prompt"),
        ({**AGENT, "model": "openai/gpt"}, "bad_value", "editor.model"),
        ({**AGENT, "model": "hermes", "max_turns": 3}, "bad_value", "editor.max_turns"),
        ({**AGENT, "permission": "root"}, "bad_value", "editor.permission"),
        ({**AGENT, "agent_bin": "not-exec"}, "bad_path", "editor.agent_bin"),
    ],
)
def test_editor_section_refusals(tmp_path, editor, code, path):
    if editor.get("agent_bin") == "not-exec":
        (tmp_path / "not-exec").write_text("#!/bin/sh\necho hi\n")
    manifest = shared_chat_manifest(tmp_path, editor)
    with pytest.raises(ManifestError) as info:
        load_manifest_v2(manifest)
    assert (info.value.code, info.value.detail) == (code, path)


@pytest.mark.parametrize(
    "edit", [lambda e: e.pop("api_key_env"), lambda e: e.update(api_key_env=None)]
)
def test_editor_api_key_env_is_optional(tmp_path, edit):
    editor = dict(CHAT)
    edit(editor)
    loaded = load_manifest_v2(shared_chat_manifest(tmp_path, editor))
    assert loaded.workflow is not None
    assert isinstance(loaded.workflow.editor, ChatEditorSection)
    assert loaded.workflow.editor.api_key_env is None


def test_lowercase_api_key_env_name_is_accepted(tmp_path):
    manifest = shared_chat_manifest(
        tmp_path, {**CHAT, "api_key_env": "lower_case_key", "proxy_env": "lower_case_key"}
    )
    loaded = load_manifest_v2(manifest)
    assert loaded.workflow is not None
    editor = loaded.workflow.editor
    assert isinstance(editor, ChatEditorSection)
    assert editor.api_key_env == "lower_case_key"
    assert editor.proxy_env == "lower_case_key"


def test_agent_bin_symlink_to_executable_is_accepted(tmp_path):
    from evolution.tree.roles.config import AgentCliEditorSection

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    real_bin = target_dir / "real-agent"
    real_bin.write_text("#!/bin/sh\necho hi\n")
    real_bin.chmod(0o755)
    manifest = shared_chat_manifest(tmp_path, {**AGENT, "agent_bin": "agent-bin-link"})
    (tmp_path / "agent-bin-link").symlink_to(real_bin)
    loaded = load_manifest_v2(manifest)
    assert loaded.workflow is not None
    editor = loaded.workflow.editor
    assert isinstance(editor, AgentCliEditorSection)
    assert editor.agent_bin == manifest.resolve().parent / "agent-bin-link"
    assert editor.agent_bin != real_bin.resolve()


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d["reflector"].pop("kind"), "missing_key: reflector"),
        (lambda d: d["reflector"].update(kind="shell"), "bad_value: reflector.kind"),
        (
            lambda d: d["reflector"].update(max_written_bytes=1),
            "unknown_key: reflector.max_written_bytes",
        ),
        (
            lambda d: d["reflector"].update(probe={"base_url": "http://127.0.0.1:8765"}),
            "bad_value: reflector.probe.base_url",
        ),
    ],
)
def test_shared_reflector_refusals(tmp_path, edit, message):
    import re

    path = shared_workflow(tmp_path)
    doc = json.loads(path.read_text())
    edit(doc)
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError, match=re.escape(message)):
        load_manifest_v2(path)


@pytest.mark.parametrize(
    ("value", "code", "where"),
    [
        ("../run-1", "bad_type", "linked_from"),
        ({"run_dir": "../run-1"}, "missing_key", "linked_from.baseline"),
        ({"baseline": "fresh"}, "missing_key", "linked_from.run_dir"),
        ({"run_dir": "", "baseline": "fresh"}, "bad_value", "linked_from.run_dir"),
        ({"run_dir": 3, "baseline": "fresh"}, "bad_type", "linked_from.run_dir"),
        ({"run_dir": "../r", "baseline": "copy"}, "bad_value", "linked_from.baseline"),
        ({"run_dir": "../r", "baseline": 3}, "bad_type", "linked_from.baseline"),
        ({"run_dir": "../r", "baseline": "fresh", "x": 1}, "unknown_key", "linked_from.x"),
    ],
)
def test_shared_linked_from_refusals(tmp_path, value, code, where):
    path = shared_workflow(tmp_path, linked_from=value)
    with pytest.raises(ManifestError) as caught:
        load_manifest_v2(path)
    assert (caught.value.code, caught.value.detail) == (code, where)


@pytest.mark.parametrize(
    ("mutate", "code", "detail"),
    [
        (lambda d: d.update(extra=1), "unknown_key", "extra"),
        (lambda d: d.pop("corpus"), "missing_key", "corpus"),
        (lambda d: d["environment"].update(kind="judge"), "bad_value", "environment.kind"),
        (lambda d: d["scorer"].update(argv=[]), "bad_value", "scorer.argv"),
        (lambda d: d["scorer"].update(timeout_seconds=0), "bad_value", "scorer.timeout_seconds"),
        (lambda d: d["editor"].update(kind="inprocess"), "bad_value", "editor.kind"),
        (lambda d: d["editor"].update(timeout_seconds=5), "unknown_key", "editor.timeout_seconds"),
        (lambda d: d["corpus"].update(path=3), "bad_type", "corpus.path"),
        (lambda d: d["scorer"].update(allowed_env=["A=B"]), "bad_value", "scorer.allowed_env[0]"),
    ],
)
def test_shared_workflow_refusals_carry_dotted_paths(tmp_path, mutate, code, detail):
    path = shared_workflow(tmp_path)
    doc = json.loads(path.read_text())
    mutate(doc)
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as info:
        load_manifest_v2(path)
    assert (info.value.code, info.value.detail) == (code, detail)


def test_reflector_requires_max_consecutive_failures(tmp_path):
    path = shared_workflow(tmp_path)
    doc = json.loads(path.read_text())
    del doc["reflector"]["max_consecutive_failures"]
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as caught:
        load_manifest_v2(path)
    assert (caught.value.code, caught.value.detail) == (
        "missing_key",
        "reflector.max_consecutive_failures",
    )


@pytest.mark.parametrize("value", [0, 21, True, 2.0, "2"])
def test_reflector_max_consecutive_failures_range(tmp_path, value):
    path = shared_workflow(tmp_path)
    doc = json.loads(path.read_text())
    doc["reflector"]["max_consecutive_failures"] = value
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as caught:
        load_manifest_v2(path)
    assert (caught.value.code, caught.value.detail) == (
        "bad_value",
        "reflector.max_consecutive_failures",
    )


@pytest.mark.parametrize("role", ["editor", "reflector"])
def test_chat_roles_require_retry(tmp_path, role):
    path = shared_workflow(tmp_path)
    doc = json.loads(path.read_text())
    section = doc[role]
    for key in ("argv", "cwd", "dependency_paths", "allowed_env", "timeout_seconds"):
        section.pop(key, None)
    section.update(CHAT, context_compact_chars=128_000, context_message_max_chars=12_000)
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as caught:
        load_manifest_v2(path)
    assert caught.value.code == "missing_key"
    assert caught.value.detail in ("editor.retry", "reflector.retry")
