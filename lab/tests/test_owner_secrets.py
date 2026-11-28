"""V-S2-7: exact target/execution secret binding with no persistence."""
from __future__ import annotations

from pathlib import Path

import pytest

from test_owner_binding import MODEL_GROUPING, make_evaluation_prepared, make_prepared


VALUES = {
    "CHAT_KEY": "chat-private-value",
    "CLI_TOKEN": "cli-private-value",
    "GROUPING_SHORT": "short",
    "GROUPING_TOKEN": "grouping-private-value",
    "OPENAI_API_KEY": "curated-private-value",
    "PROXY_URL": "https://proxy.invalid/private",
    "SCORE_TOKEN": "score-private-value",
    "SHARED_TOKEN": "same-private-value",
    "TARGET_TOKEN": "target-private-value",
}


def _binding(tmp_path: Path):
    from evolution_lab.owner.binding import open_prepared
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_prepared(tmp_path, secret_roles=True)
    return saved, open_prepared(
        PreparationStorage(saved["state"]), saved["prepared"].digest
    ).bundle


def test_partition_uses_public_core_role_bindings_and_leaves_unused_names(
    tmp_path: Path,
) -> None:
    from evolution_lab.owner.secrets import secret_partition

    _, binding = _binding(tmp_path)
    partition = secret_partition(binding)

    assert partition.declared == (
        "CHAT_KEY",
        "CLI_TOKEN",
        "GROUPING_SHORT",
        "GROUPING_TOKEN",
        "OPENAI_API_KEY",
        "PROXY_URL",
        "SCORE_TOKEN",
        "SHARED_TOKEN",
        "TARGET_TOKEN",
        "UNUSED_TOKEN",
    )
    assert partition.target == ("SHARED_TOKEN", "TARGET_TOKEN")
    assert partition.execution == (
        "CHAT_KEY",
        "CLI_TOKEN",
        "GROUPING_SHORT",
        "GROUPING_TOKEN",
        "OPENAI_API_KEY",
        "PROXY_URL",
        "SCORE_TOKEN",
        "SHARED_TOKEN",
    )
    assert partition.unused == ("UNUSED_TOKEN",)
    assert "OPTIONAL_NONSECRET" not in partition.execution
    assert "OPTIONAL_GROUPING_NONSECRET" not in partition.execution


def _model_grouping_binding(tmp_path: Path):
    from evolution_lab.owner.binding import open_prepared
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_prepared(tmp_path, model_grouping=MODEL_GROUPING)
    return open_prepared(PreparationStorage(saved["state"]), saved["prepared"].digest).bundle


def test_model_grouping_key_and_proxy_are_execution_secrets(tmp_path: Path) -> None:
    from evolution_lab.owner.secrets import secret_partition

    partition = secret_partition(_model_grouping_binding(tmp_path))

    assert {"GROUPING_KEY", "GROUPING_PROXY"} <= set(partition.execution)
    assert not {"GROUPING_KEY", "GROUPING_PROXY"} & set(partition.unused)
    assert "GROUPING_KEY" not in partition.target


def test_exact_secret_sets_build_closed_environments_without_ambient_values(
    tmp_path: Path,
) -> None:
    from evolution_lab.owner.secrets import bind_execution_secrets, bind_target_secrets

    saved, binding = _binding(tmp_path)
    target = bind_target_secrets(
        binding,
        {name: VALUES[name] for name in ("SHARED_TOKEN", "TARGET_TOKEN")},
    )
    execution = bind_execution_secrets(
        binding,
        {name: VALUES[name] for name in (
            "CHAT_KEY", "CLI_TOKEN", "GROUPING_SHORT", "GROUPING_TOKEN",
            "OPENAI_API_KEY", "PROXY_URL",
            "SCORE_TOKEN", "SHARED_TOKEN",
        )},
        target_secrets=target,
    )

    assert dict(target.environment) == {
        "TARGET_MODE": "local",
        "SHARED_TOKEN": VALUES["SHARED_TOKEN"],
        "TARGET_TOKEN": VALUES["TARGET_TOKEN"],
    }
    assert dict(execution.environment) == {
        "BASE_MODE": "saved",
        "TARGET_MODE": "local",
        **{name: VALUES[name] for name in execution.names},
    }
    assert "PATH" not in execution.environment
    state_bytes = b"".join(
        path.read_bytes() for path in saved["state"].rglob("*") if path.is_file()
    )
    for value in VALUES.values():
        assert value.encode() not in state_bytes


@pytest.mark.parametrize(
    "kind,values,code,stage",
    [
        ("target", {"TARGET_TOKEN": "target-private-value"}, "invalid_argument", "target_start"),
        ("target", {"SHARED_TOKEN": "x", "TARGET_TOKEN": "y", "EXTRA": "z"}, "invalid_argument", "target_start"),
        ("execution", {name: value for name, value in VALUES.items() if name != "TARGET_TOKEN" and name != "SHARED_TOKEN"}, "invalid_argument", "before_start"),
    ],
)
def test_secret_keysets_are_exact(
    tmp_path: Path, kind: str, values: dict[str, str], code: str, stage: str
) -> None:
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.owner.secrets import bind_execution_secrets, bind_target_secrets

    _, binding = _binding(tmp_path)
    with pytest.raises(OwnerError) as caught:
        if kind == "target":
            bind_target_secrets(binding, values)
        else:
            target = bind_target_secrets(binding, {
                "SHARED_TOKEN": VALUES["SHARED_TOKEN"],
                "TARGET_TOKEN": VALUES["TARGET_TOKEN"],
            })
            bind_execution_secrets(binding, values, target_secrets=target)

    assert caught.value.code == code
    assert caught.value.stage == stage


def test_shared_target_and_execution_value_must_match(tmp_path: Path) -> None:
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.owner.secrets import bind_execution_secrets, bind_target_secrets

    _, binding = _binding(tmp_path)
    target = bind_target_secrets(binding, {
        "SHARED_TOKEN": "target-copy",
        "TARGET_TOKEN": VALUES["TARGET_TOKEN"],
    })
    execution_values = {
        name: VALUES[name]
        for name in (
            "CHAT_KEY", "CLI_TOKEN", "GROUPING_SHORT", "GROUPING_TOKEN",
            "OPENAI_API_KEY", "PROXY_URL", "SCORE_TOKEN", "SHARED_TOKEN",
        )
    }

    with pytest.raises(OwnerError) as caught:
        bind_execution_secrets(binding, execution_values, target_secrets=target)

    assert caught.value.code == "binding_mismatch"
    assert caught.value.stage == "before_start"


def test_worker_adds_only_long_grouping_values_to_its_redaction_set(
    tmp_path: Path,
) -> None:
    from evolution_lab.owner._campaign_worker import (
        _worker_redactions,
        _worker_short_forwarded_names,
    )

    _, binding = _binding(tmp_path)
    environment = {name: VALUES[name] for name in binding.secret_names if name in VALUES}

    redactions = _worker_redactions(binding.manifest, environment, ("core-secret-value",))

    assert redactions == ("core-secret-value", VALUES["GROUPING_TOKEN"])
    assert VALUES["GROUPING_SHORT"] not in redactions
    assert _worker_short_forwarded_names(binding.manifest, environment, ()) == (
        "GROUPING_SHORT",
    )


@pytest.mark.parametrize("text,prefix", [
    ("check shadowed: connection_preflight: ", "evo tree evolve: "),
    ("check connection_preflight is off: ", "evo tree evolve: note: "),
])
def test_worker_check_notes_redact_multiline_values(tmp_path, monkeypatch, capsys, text, prefix):
    """Both check-note paths keep the worker's redactor around the complete line."""
    import asyncio
    from types import SimpleNamespace

    import evolution.tree.evolve as evolve_module
    from evolution_lab.owner import _campaign_worker as worker, core_raw

    secret = "neutral-secret\nsecond-secret-line"
    admitted = SimpleNamespace(workflow=SimpleNamespace(checks=None), target=object())
    monkeypatch.setattr(evolve_module, "unencrypted_model_roles", lambda _: ())
    monkeypatch.setattr(evolve_module, "unauthenticated_roles", lambda _: ())
    monkeypatch.setattr(core_raw, "check_mode_notes", lambda _: [text + secret])
    monkeypatch.setattr(worker, "settle_timeout", lambda _: .01)

    class BeforeExecution(Exception):
        pass

    async def stop_before_execution(*args, **kwargs):
        kwargs["notes"](text + secret)
        raise BeforeExecution

    monkeypatch.setattr(evolve_module, "evolve", stop_before_execution)
    config = {"run_id": "neutral-run", "run_directory": str(tmp_path),
              "settlement_path": str(tmp_path / "settlement.json"),
              "scope_id": "1" * 32, "owner_generation": "2" * 32, "operation_id": "3" * 32}
    with pytest.raises(BeforeExecution):
        asyncio.run(worker._evolve(config, {}, lambda: False, admitted, (secret,), ()))
    assert capsys.readouterr().err == (prefix + text + "[REDACTED]\n") * 2


def test_model_grouping_values_are_redacted(tmp_path: Path) -> None:
    from evolution_lab.owner._campaign_worker import _worker_redactions

    binding = _model_grouping_binding(tmp_path)
    environment = {"GROUPING_KEY": "sk-very-secret-value", "GROUPING_PROXY": "http://p:1/xy"}

    assert {"sk-very-secret-value", "http://p:1/xy"} <= set(
        _worker_redactions(binding.manifest, environment, ())
    )


def test_short_model_grouping_value_is_a_short_forwarded_name(tmp_path: Path) -> None:
    from evolution_lab.owner._campaign_worker import (
        _worker_redactions,
        _worker_short_forwarded_names,
    )

    binding = _model_grouping_binding(tmp_path)
    environment = {"GROUPING_KEY": "short", "GROUPING_PROXY": "http://p:1/xy"}

    assert "GROUPING_KEY" in _worker_short_forwarded_names(binding.manifest, environment, ())
    assert "short" not in _worker_redactions(binding.manifest, environment, ())


def test_fields_grouping_forwards_no_names(tmp_path: Path) -> None:
    from evolution_lab.owner._campaign_worker import _grouping_forwarded_names

    _, binding = _binding(tmp_path)
    fields = make_prepared(tmp_path / "fields")
    from evolution_lab.owner.binding import open_prepared
    from evolution_lab.preparation.storage import PreparationStorage

    opened = open_prepared(PreparationStorage(fields["state"]), fields["prepared"].digest).bundle
    assert _grouping_forwarded_names(opened.manifest, {"GROUPING_KEY": "x" * 12}) == ()


@pytest.mark.parametrize(
    "location",
    ["base_env", "target_env", "declared_secret", "target_secret"],
)
def test_service_fd_names_are_refused_from_saved_inputs(tmp_path: Path, location: str) -> None:
    from evolution_lab.owner.binding import open_bundle
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_prepared(
        tmp_path, secret_roles=True, service_location=location
    )

    with pytest.raises(OwnerError) as caught:
        open_bundle(PreparationStorage(saved["state"]), saved["bundle"].digest)

    assert caught.value.code == "artifacts_invalid"
    assert caught.value.stage == "connect"


def test_evaluation_uses_only_declared_scorer_secrets_and_conformance_uses_none(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner.binding import open_prepared
    from evolution_lab.owner.secrets import (
        bind_conformance_secrets,
        bind_execution_secrets,
        bind_target_secrets,
        secret_partition,
    )
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_evaluation_prepared(tmp_path, monkeypatch, shared_secret=True)
    binding = open_prepared(
        PreparationStorage(saved["state"]), saved["prepared"].digest
    ).bundle
    partition = secret_partition(binding)
    assert partition.declared == ("SCORER_TOKEN", "TARGET_TOKEN")
    assert partition.target == ("TARGET_TOKEN",)
    assert partition.execution == ("SCORER_TOKEN", "TARGET_TOKEN")
    target = bind_target_secrets(binding, {"TARGET_TOKEN": "shared-value"})
    execution = bind_execution_secrets(
        binding,
        {"SCORER_TOKEN": "score-value", "TARGET_TOKEN": "shared-value"},
        target_secrets=target,
    )
    assert dict(execution.environment) == {
        "MODE": "offline",
        "SCORER_TOKEN": "score-value",
        "TARGET_TOKEN": "shared-value",
    }
    conformance = bind_conformance_secrets(binding)
    assert conformance.names == ()
    assert dict(conformance.values) == {}
    assert dict(conformance.environment) == {}


def test_campaign_derived_evaluation_rebinds_only_scorer_and_uses_prepared_secret_set(
    tmp_path: Path
) -> None:
    from evolution_lab.owner.binding import open_prepared, rebind_evaluation_input
    from evolution_lab.owner.secrets import bind_execution_secrets, bind_target_secrets
    from evolution_lab.preparation import PreparationStore
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_prepared(tmp_path, secret_roles=True)
    prepared = PreparationStore(saved["state"]).prepare_evaluation(
        saved["bundle"].digest,
        selection={"count": 1},
        repeat_seeds=[0],
        evaluation_set_id="fixed-set",
        max_turns=2,
        timeout_seconds=3,
        metric_plan={"primary": "score"},
        max_failed_turns=1,
        call_retries="off",
    )
    bound = open_prepared(PreparationStorage(saved["state"]), prepared.digest)
    environment, scorer, _ = rebind_evaluation_input(bound.bundle)
    assert environment.identity.kind == "static"
    assert scorer.allowed_env == frozenset(
        {"OPTIONAL_NONSECRET", "SCORE_TOKEN", "SHARED_TOKEN"}
    )
    target = bind_target_secrets(
        bound.bundle,
        {"SHARED_TOKEN": "same", "TARGET_TOKEN": "target"},
    )
    execution = bind_execution_secrets(
        bound.bundle,
        {"SCORE_TOKEN": "score", "SHARED_TOKEN": "same"},
        target_secrets=target,
        kind=bound.kind,
    )
    assert execution.names == ("SCORE_TOKEN", "SHARED_TOKEN")
    assert "CHAT_KEY" not in execution.environment


def test_secret_partition_and_preparation_share_declared_names(tmp_path, monkeypatch):
    from evolution_lab.owner import secrets
    original = secrets.declared_environment_names
    calls = []
    def declared(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(secrets, 'declared_environment_names', declared)
    # This fixture's forwarded names are all explicit declarations; the other
    # partition fixture separately retains implicit OPENAI_API_KEY forwarding.
    binding = _model_grouping_binding(tmp_path)
    assert calls, 'Bundle preparation must use the shared declaration collector'
    before = len(calls)
    partition = secrets.secret_partition(binding)
    assert len(calls) > before, 'the secret partition must use the same collector'
    names = original(workflow=binding.manifest.workflow, launch=binding.launch)
    assert set(partition.execution) == names.execution & set(binding.secret_names)
    assert set(partition.target) == names.target_secrets
    assert names.target_static == frozenset(binding.launch['env'])
