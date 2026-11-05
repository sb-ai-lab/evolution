"""Reader refusals for removed campaign families (PDR-0038, ADR-0046)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.tree.campaign import (
    CampaignFamilyUnsupported,
    CampaignMissing,
    _unreadable_view,
    inspect_campaign,
)
from evolution.tree.campaign_projections import (
    CampaignUnreadable,
    project_status,
    read_family_version,
    read_run_id,
)
from evolution.tree.campaign_schema import CampaignLifecycle, reduce
from evolution.tree.cli import tree_app
from evolution.tree.run_reader import RunKind, RunRefused, open_run
from evolution.tree.run_watch import WatchSession
from evolution.tree.storage.envelope import SchemaError
from evolution.tree.storage.journal_errors import JournalFault

REMOVED_FAMILY_EXPECTED = (
    "this release reads campaign families 14 and 15; read this run with evolution "
    "2.0.0b9 or seed a new /15 campaign from its champion copy"
)


def test_native_final_grammar_keeps_its_exact_keys_and_selection_reasons():
    from evolution.tree.campaign_schema import _FINAL_KEYS, campaign_family_for
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.final_report import SELECTION_REASONS14

    # Captured on d34542a9 before B5 materializes the inherited grammar.
    assert (
        hashlib.sha256(
            canonical_json([[event, sorted(keys)] for event, keys in _FINAL_KEYS.items()])
        ).hexdigest()
        == "35105b6b93a2277e269e43fc6c48deb0550796057838c8558191e6808ce0adef"
    )
    assert SELECTION_REASONS14 == {
        "finalist_selected",
        "global_bound_not_positive",
        "global_point_not_improved",
        "no_accepted_finalist",
        "no_finalists",
    }
    payload = dict(
        round=1,
        attempt_id=None,
        candidate_digest=None,
        dependency_digest=None,
        measurement_id=None,
        global_="off",
        reason="no_accepted_finalist",
        ranking=[],
        evidence=[
            dict(
                version=1,
                kind="journal_prefix",
                run_id="run",
                locator="measurements/m/events.jsonl#bytes=0:1",
                content_digest="a" * 64,
            )
        ],
    )
    payload["global"] = payload.pop("global_")
    campaign_family_for(14).validate_payload("selection_decided", payload)
    payload["reason"] = "no_eligible_finalist"
    with pytest.raises(SchemaError, match="reason is not in the registry"):
        campaign_family_for(14).validate_payload("selection_decided", payload)


def test_snapshot_and_evidence_require_explicit_keyword_family_context():
    import inspect

    from evolution.tree.campaign_projections import _Evidence
    from evolution.tree.campaign_schema import CampaignSnapshot, _validate_created

    for constructor in (CampaignSnapshot, _Evidence, _validate_created):
        parameter = inspect.signature(constructor).parameters["family_version"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


def test_retired_envelopes_and_layered_gate_are_removed():
    import importlib.util

    from evolution import tree
    from evolution.tree import campaign_schema

    for prefix in (
        "CAMPAIGN",
        "ADAPTIVE_CAMPAIGN",
        "LAYERED_CAMPAIGN",
        "CONTINUABLE_CAMPAIGN",
        "SUSPENDABLE_CAMPAIGN",
        "DIAGNOSTIC_CAMPAIGN",
        "MODEL_GROUPING_CAMPAIGN",
        "RESILIENCE_CAMPAIGN",
        "SPLIT_CAMPAIGN",
        "AFFECTED_CAMPAIGN",
        "CONFIDENCE_CAMPAIGN",
    ):
        for suffix in ("_FAMILY", "_FAMILY_VERSION"):
            name = prefix + suffix
            assert not hasattr(campaign_schema, name)
            assert not hasattr(tree, name)
    assert importlib.util.find_spec("evolution.tree.campaign_gate") is None


def test_independent_shared_member_accessors_keep_the_generic_final_non_match():
    from evolution.tree.campaign_schema import CampaignLifecycle, RoundState

    life = CampaignLifecycle(selector=None, family_version=15)
    life._rounds[1] = RoundState(round=1)
    generic = dict(role="final", round=1, measurement_id="generic", complete=True)
    life._measurements["generic"] = generic
    assert life._cohort_member("generic") is None
    assert not life._cohort_measurement(generic)
    assert not life._completed_member(generic)
    owned = {**generic, "role": "family15_final"}
    life._measurements["owned"] = owned
    assert life._cohort_member("owned") is None
    assert life._cohort_measurement(owned)
    assert life._completed_member(owned)


async def test_v15_and_v16_policy_and_identity_digests_are_unchanged(tmp_path):
    from evolution.tree.campaign_policy import CampaignPolicyV11, CampaignPolicyV12
    from evolution.tree.reference_target_cli import render_sample_manifest
    from tests.campaign_fakes import Rig
    from tests.family15_fakes import v16_document

    # Captured on 6275834b2 before B4 changes any source or policy builder.
    # Bind each document's policy in the same native Rig so paths, clocks and
    # provider processes cannot affect the campaign-identity comparison.
    policies = {
        "scaffold_v15": CampaignPolicyV11.from_payload(
            json.loads(render_sample_manifest())["campaign"]
        ),
        "test_v16": CampaignPolicyV12.from_payload(v16_document()["campaign"]),
        "default_rig": None,
    }
    expected = {
        "scaffold_v15": (
            "55feb22cd105c56792438112cbe707dd3aa26212f5d524e791569591e65c4008",
            "f8319bb9335a336d1fca4823d0e816f2770c10882ce23b196d5abe395bfdf436",
        ),
        "test_v16": (
            "d1b0f82021dd439eac4e4b6a87786c070abc5be65100575890b47a2b9956602b",
            "71f918b0e49435fa9be7173b52cc6bb8b9e53a1501d3177f75ca1931b02f700a",
        ),
        "default_rig": (
            "1b0a600cc320e54726246992a1965ccad8f63029c9f4520e1a7bc497507499f9",
            "52eaa5ef18490d995a19eb8726df0bd1897b2027ca67a196e3cdaff9e4d69dd9",
        ),
    }
    for name, policy in policies.items():
        rig = Rig(tmp_path / name, campaign_policy=policy)
        await rig.run(crash_after="campaign_created")
        created = rig.records()[0]["payload"]
        assert (rig.policy.digest, created["identity_digest"]) == expected[name]


def test_native_policy_field_and_payload_order_is_unchanged():
    from dataclasses import fields

    from evolution.tree.campaign_policy import AlphaLedgerUnavailable
    from tests.campaign_fakes import v11_policy

    policy = v11_policy()
    # Captured alongside the digest pins on 6275834b2; canonical JSON sorts
    # keys, so the digest alone cannot prove the public payload/field order.
    assert [field.name for field in fields(policy)] == (
        "rounds attempts_per_round parallel_attempts _alpha_budget _alpha_spend "
        "indeterminate budget max_concurrency max_scorer_concurrency metric_primary "
        "random_seed editor_timeout_seconds editor_context reflection_timeout_seconds "
        "_regression_margin gate_provenance fresh_parent global_layer gate_mode "
        "repeats_per_question focus_confidence causal_confidence causal_tolerance "
        "global_mode round_stop baseline_refresh_after_rounds stability focus_margin "
        "attempt_feedback sample_retries declares_failed_turns declares_call_retries _alpha_plan"
    ).split()
    assert (
        list(policy.to_payload())
        == (
            "rounds attempts_per_round parallel_attempts evaluation indeterminate budget "
            "max_concurrency max_scorer_concurrency metric random_seed editor_timeout_seconds "
            "editor_context reflection_timeout_seconds gate attempt_feedback sample_retries "
            "call_retries round_stop baseline_refresh_after_rounds"
        ).split()
    )
    assert (
        list(policy.to_payload()["gate"])
        == ("mode focus causal provenance fresh_parent global stability").split()
    )
    assert not policy.declares_alpha
    for name in ("alpha_budget", "alpha_spend", "regression_margin", "alpha_plan"):
        with pytest.raises(AlphaLedgerUnavailable, match=f"^alpha_ledger_unavailable: {name}$"):
            getattr(policy, name)


async def test_a_damaged_native_policy_remains_an_identity_defect(tmp_path):
    from evolution.tree.campaign_identity import CampaignIdentityV1
    from evolution.tree.campaign_policy import (
        CampaignPolicyV11,
        PolicyError,
        policy_class_for_payload,
    )
    from evolution.tree.execution.journal_identity import IdentityError, canonical_json
    from tests.campaign_fakes import Rig

    rig = Rig(tmp_path)
    await rig.run(crash_after="campaign_created")
    identity = rig.records()[0]["payload"]["identity"]
    del identity["policy"]["round_stop"]
    identity["policy_digest"] = hashlib.sha256(canonical_json(identity["policy"])).hexdigest()
    assert policy_class_for_payload(identity["policy"]) is CampaignPolicyV11
    with pytest.raises(PolicyError, match="^missing_key: round_stop$"):
        CampaignPolicyV11.from_payload(identity["policy"])
    with pytest.raises(IdentityError, match="^invalid_identity_policy$"):
        CampaignIdentityV1.from_payload(identity)


@pytest.mark.parametrize("version", [11, 12])
def test_misfiled_native_profiles_keep_their_diagnostic_family_labels(version):
    from evolution.tree import campaign_policy
    from evolution.tree.campaign import (
        PolicyNotAdmitted,
        _family_for_policy_class,
        _refuse_misfiled,
    )

    # Diagnostic labels only, captured before flattening: these profiles never
    # admit a native writer. The 13 boundary still classifies corrupt evidence.
    policy_class = getattr(campaign_policy, f"CampaignPolicyV{version}")
    selected = _family_for_policy_class(policy_class, "campaign_adaptation/6")
    assert selected == 13
    with pytest.raises(PolicyNotAdmitted, match="^policy_v11_not_admitted$"):
        _refuse_misfiled(selected, 14)


@pytest.mark.parametrize(
    ("family", "code"),
    [
        (7, "adaptive_runtime_policy_v10_family_mismatch"),
        (11, "adaptive_runtime_policy_v10_family_mismatch"),
        (13, "adaptive_runtime_policy_v11_family_mismatch"),
    ],
)
def test_native_policy_profile_mismatches_keep_their_typed_refusal(family, code):
    from evolution.tree.campaign import _admit_runtime_policy
    from tests.campaign_fakes import v11_policy

    with pytest.raises(ValueError, match=f"^{code}$"):
        _admit_runtime_policy(v11_policy(), family)


@pytest.mark.parametrize("schema", [f"evolution-v2-manifest/{n}" for n in range(1, 15)])
@pytest.mark.parametrize("entry", ["loader", "evolve", "conformance", "serve", "trace"])
async def test_every_entry_point_refuses_an_old_manifest(tmp_path, monkeypatch, schema, entry):
    from evolution.tree import conformance, reference_target_cli
    from evolution.tree.evolve import EvolveRefused, admit_manifest
    from evolution.tree.execution.config import ManifestError
    from evolution.tree.manifest.manifest_v2 import load_manifest_v2

    manifest = tmp_path / "manifest.json"
    # Admission must precede all section parsing, program loading and target calls.
    manifest.write_text(json.dumps({"schema": schema}))
    expected = "expected evolution-v2-manifest/15 or /16"

    def unexpected(*args, **kwargs):
        pytest.fail("an old manifest contacted a target")

    monkeypatch.setattr(conformance, "HttpTransport", unexpected)
    if entry in ("loader", "evolve"):
        with pytest.raises(ManifestError if entry == "loader" else EvolveRefused) as caught:
            (load_manifest_v2 if entry == "loader" else admit_manifest)(manifest)
        assert caught.value.code == "manifest_schema"
        assert caught.value.expected == expected
        return
    if entry == "trace":
        from tests.campaign_fakes import Rig

        rig = Rig(tmp_path / "native")
        await rig.run(crash_after="campaign_created")
        argv = ["trace", "--run-dir", str(rig.run_root), "--manifest", str(manifest), "--out", "-"]
        app = tree_app
    elif entry == "serve":
        argv = ["serve", str(manifest)]
        app = reference_target_cli.app
    else:
        argv = ["conformance", "--manifest", str(manifest)]
        app = tree_app
    before = _tree_bytes(tmp_path)
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 2, (result.output, result.exception)
    assert "manifest_schema" in result.stderr
    assert expected in result.stderr
    assert "Traceback" not in result.output
    assert _tree_bytes(tmp_path) == before


def old_run_dir(tmp_path: Path, family: int | None) -> Path:
    """Hand-build only the creation envelope; no retired writer is needed."""
    run = tmp_path / "old-run"
    run.mkdir()
    payload = {
        "identity": {},
        "identity_digest": "a" * 64,
        "producing_version": "2.0.0b9",
        "alpha_attempt": None,
    }
    if family is not None:
        payload["family_version"] = family
    record = {
        "schema_version": 1,
        "run_id": "old-run",
        "sequence": 1,
        "timestamp": "2026-10-20T00:00:00Z",
        "event": "campaign_created",
        "operation_id": None,
        "payload": payload,
    }
    (run / "campaign.jsonl").write_text(json.dumps(record) + "\n")
    assert read_family_version(run) == family
    return run


@pytest.mark.parametrize("family", [3, 5, 6, 12, 13, None])
def test_an_old_run_dir_is_refused_by_every_library_reader(tmp_path, family):
    run = old_run_dir(tmp_path, family)
    label = "absent" if family is None else str(family)
    for read in (
        lambda: inspect_campaign(run),
        lambda: inspect_campaign(run, verify=True),
        lambda: read_run_id(run),
        lambda: open_run(run),
    ):
        with pytest.raises(CampaignFamilyUnsupported) as caught:
            read()
        assert caught.value.journaled == family
        assert str(caught.value) == "campaign_family_unsupported"
        assert caught.value.args == ("campaign_family_unsupported",)
        assert caught.value.path == f"family/{label}"
        assert caught.value.expected == REMOVED_FAMILY_EXPECTED


@pytest.mark.parametrize("family", [3, 5, 6, 12, 13, None, 99])
def test_the_writer_family_refusal_uses_the_same_code_and_attributes(family):
    from evolution.tree.campaign import _refuse_unadmitted_family

    with pytest.raises(CampaignFamilyUnsupported) as caught:
        _refuse_unadmitted_family(family)
    assert str(caught.value) == "campaign_family_unsupported"
    assert caught.value.args == ("campaign_family_unsupported",)
    assert caught.value.journaled == family
    assert caught.value.reader == 14
    assert caught.value.path == f"family/{'absent' if family is None else family}"
    assert caught.value.expected == (None if family == 99 else REMOVED_FAMILY_EXPECTED)


def _assert_illegible_base_results(run):
    # Captured on e16e3b5fe before B1: no legible creation is not an old family.
    for read in (read_family_version, read_run_id):
        with pytest.raises(CampaignUnreadable, match="^campaign_unreadable$"):
            read(run)
    for verify in (False, True):
        with pytest.raises(SchemaError, match="^invalid public projection$"):
            inspect_campaign(run, verify=verify)
    handle = open_run(run)
    assert handle.kind is RunKind.CAMPAIGN
    assert handle.records == ()
    assert handle.valid_bytes == 0
    assert handle.snapshot.run_id == ""
    assert handle.snapshot.identity == {}
    session = WatchSession(run)
    try:
        assert session.snapshot.lines() == []
        assert session.snapshot.snapshot_line() is None
        assert session.records == []
        assert session.heads == {}
        assert session.initial_ending is None
        assert session.poll() == ([], None)
    finally:
        session.close()
    result = CliRunner().invoke(tree_app, ["watch", "--run-dir", str(run), "--once"])
    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")


def test_an_illegible_creation_line_keeps_its_unreadable_result(tmp_path):
    (tmp_path / "campaign.jsonl").write_bytes(b'{"not json')
    _assert_illegible_base_results(tmp_path)


def test_a_partial_family_14_creation_line_is_not_refused_as_old(tmp_path):
    source = Path(__file__).parent / "fixtures" / "family14_b2" / "run" / "campaign.jsonl"
    with source.open("rb") as journal:
        creation = journal.readline()
    assert json.loads(creation)["payload"]["family_version"] == 14
    (tmp_path / "campaign.jsonl").write_bytes(creation[:40])
    _assert_illegible_base_results(tmp_path)


def test_a_missing_creation_journal_keeps_its_base_results(tmp_path):
    for read in (read_family_version, read_run_id):
        with pytest.raises(CampaignMissing, match="^campaign_absent$"):
            read(tmp_path)
    for verify in (False, True):
        with pytest.raises(JournalFault, match="^journal_unavailable$"):
            inspect_campaign(tmp_path, verify=verify)
    with pytest.raises(RunRefused, match="^not_a_run_directory$"):
        open_run(tmp_path)


@pytest.mark.parametrize("adaptive", [False, True])
def test_an_unspecified_lifecycle_and_reducer_family_defaults_to_14(adaptive):
    state = CampaignLifecycle(selector=None, adaptive=adaptive, family_version=None)
    assert state.snapshot.family_version == 14
    assert reduce((), selector=None, adaptive=adaptive).family_version == 14


def test_the_unreadable_view_uses_an_empty_family_14_snapshot(tmp_path):
    view = _unreadable_view(tmp_path)
    assert view.snapshot.family_version == 14
    assert view.snapshot.identity == {}
    assert view.snapshot.run_id == ""
    assert view.measurements == {}
    assert view.lineage is None
    # The reporting caller can supply its own id, but no creation facts. Keep
    # the base's minimal status rather than inventing role or policy settings.
    view = replace(view, snapshot=replace(view.snapshot, run_id="c-unreadable"))
    assert project_status(view).to_payload() == {
        "schema_version": 1,
        "kind": "campaign_status",
        "run_id": "c-unreadable",
        "identity_digest": None,
        "rebases": [],
        "consistency": "durable",
        "complete": False,
        "claim_scope": "campaign",
        "campaign_outcome": None,
        "final_champion": None,
        "rounds": [],
        "alpha": {"per_round": {}, "per_attempt": {}},
        "indeterminate": {
            "counts": {"target": 0, "environment": 0, "scorer": 0, "rollout": 0},
            "denominator": 0,
            "threshold_reached": None,
        },
        "measurements": [],
        "siblings": {"artifact_store": "absent"},
        "lineage": None,
        "actions": [],
        "verification": None,
    }


def _tree_bytes(root):
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("family", [12, None])
@pytest.mark.parametrize("command", ["watch", "trace", "authorize", "authorize-write"])
def test_the_cli_refuses_an_old_run_dir_with_its_family(tmp_path, command, family):
    run = old_run_dir(tmp_path, family)
    before = _tree_bytes(tmp_path)
    argv = {
        "watch": ["watch", "--run-dir", str(run), "--once"],
        "trace": ["trace", "--run-dir", str(run), "--out", str(tmp_path / "trace.jsonl")],
        "authorize": ["authorize", str(run), "--list"],
        "authorize-write": [
            "authorize",
            str(run),
            "run_identity_changed",
            "--digest",
            "a" * 64,
            "--reason",
            "repair",
        ],
    }[command]
    result = CliRunner().invoke(tree_app, argv)
    label = "absent" if family is None else str(family)
    assert result.exit_code == 2, result.exception
    assert result.stdout == ""
    assert result.stderr == (
        f"evo tree {argv[0]}: refused: campaign_family_unsupported at family/{label}; "
        f"expected {REMOVED_FAMILY_EXPECTED}\n"
    )
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("batch", [False, True])
def test_authorize_on_an_old_run_dir_writes_nothing(tmp_path, monkeypatch, batch):
    from evolution.tree import cli

    run = old_run_dir(tmp_path, 12)
    before = _tree_bytes(run)

    def unexpected(*args, **kwargs):
        pytest.fail("authorize read decisions or scanned the journal before checking its family")

    monkeypatch.setattr(cli, "read_decisions", unexpected)
    monkeypatch.setattr(cli.evolve_module, "journaled_authorization_ids", unexpected)
    args = (
        ["--accept", "run_identity_changed:" + "a" * 64]
        if batch
        else ["run_identity_changed", "--digest", "a" * 64]
    )
    result = CliRunner().invoke(tree_app, ["authorize", str(run), *args, "--reason", "repair"])
    assert _tree_bytes(run) == before
    assert result.exit_code == 2, result.exception
    assert result.stderr == (
        "evo tree authorize: refused: campaign_family_unsupported at family/12; "
        f"expected {REMOVED_FAMILY_EXPECTED}\n"
    )


def test_authorize_list_checks_family_before_scanning_decisions(tmp_path, monkeypatch):
    from evolution.tree import cli

    run = old_run_dir(tmp_path, 12)

    def unexpected(*args, **kwargs):
        pytest.fail("authorize --list scanned before checking its family")

    monkeypatch.setattr(cli, "read_decisions", unexpected)
    monkeypatch.setattr(cli.evolve_module, "journaled_authorization_ids", unexpected)
    result = CliRunner().invoke(tree_app, ["authorize", str(run), "--list"])
    assert result.exit_code == 2, result.exception
    assert "campaign_family_unsupported at family/12" in result.stderr


@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("family", [12, None])
def test_evaluate_from_an_old_campaign_run_dir_refuses_without_a_traceback(
    tmp_path, progress, family
):
    from tests.test_tree_train_test import evaluation_manifest

    run = old_run_dir(tmp_path, family)
    manifest = evaluation_manifest(tmp_path / "workspace")
    document = json.loads(manifest.read_text())
    document["snapshot"] = {"run_dir": str(run), "version": "v0"}
    manifest.write_text(json.dumps(document))
    before = _tree_bytes(tmp_path)
    output = tmp_path / "evaluation-run"
    result = CliRunner().invoke(
        tree_app,
        ["evaluate", "--manifest", str(manifest), "--run-dir", str(output)]
        + (["--progress"] if progress else []),
    )
    assert result.exit_code == 2, result.exception
    assert result.stdout == ""
    label = "absent" if family is None else str(family)
    assert result.stderr == (
        f"evo tree evaluate: refused: campaign_family_unsupported at family/{label}; "
        f"expected {REMOVED_FAMILY_EXPECTED}\n"
    )
    assert "Traceback" not in result.output
    assert _tree_bytes(tmp_path) == before


def _evolve_family_stderr(family):
    # Full stderr captured on Part B merge 1bbc391cb, before the R1 correction.
    label = "absent" if family is None else str(family)
    return (
        f"evo tree evolve: refused: campaign_family_unsupported at family/{label}\n"
        f"evo tree evolve: {REMOVED_FAMILY_EXPECTED}\n"
        "evo tree evolve: class: hard\n"
        "evo tree evolve: the journal is of another record family and there is no converter\n"
    ).encode()


@pytest.mark.parametrize("family", [12, None])
@pytest.mark.parametrize("progress", [False, True])
def test_evolve_keeps_the_full_approved_family_diagnostic(tmp_path, family, progress):
    from tests.tree_evolve_support import write_workspace

    run = old_run_dir(tmp_path, family)
    manifest = write_workspace(tmp_path / "workspace")
    before = _tree_bytes(tmp_path)
    result = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(manifest), "--run-dir", str(run)]
        + (["--progress"] if progress else []),
    )
    assert (result.exit_code, result.stdout_bytes, result.stderr_bytes) == (
        2,
        b"",
        _evolve_family_stderr(family),
    )
    assert _tree_bytes(tmp_path) == before


def _replace_before_read_run_id(monkeypatch, run, replacement):
    from evolution.tree import evolve as evolve_module

    original = evolve_module.read_run_id
    probes = []
    after_replacement = {}

    def replace_then_read(root):
        # The real gate must see native family 14 before the reader sees the
        # replacement: bypassing the gate would not exercise the reported race.
        assert root == run
        probes.append(read_family_version(root))
        assert probes == [14]
        (root / "campaign.jsonl").write_bytes(replacement)
        after_replacement.update(_tree_bytes(run.parent))
        return original(root)

    monkeypatch.setattr(evolve_module, "read_run_id", replace_then_read)
    return probes, after_replacement


@pytest.mark.parametrize("family", [12, None])
async def test_a_reader_family_replacement_is_an_evolve_refusal(tmp_path, monkeypatch, family):
    from evolution.tree.evolve import EvolveRefused, _journaled_run_id
    from tests.campaign_fakes import Rig

    rig = Rig(tmp_path / "native")
    await rig.run(crash_after="campaign_created")
    replacement = (old_run_dir(tmp_path, family) / "campaign.jsonl").read_bytes()
    probes, after = _replace_before_read_run_id(monkeypatch, rig.run_root, replacement)
    with pytest.raises(EvolveRefused) as caught:
        _journaled_run_id(rig.run_root)
    assert probes == [14]
    assert caught.value.code == "campaign_family_unsupported"
    assert caught.value.path == f"family/{'absent' if family is None else family}"
    assert caught.value.expected == REMOVED_FAMILY_EXPECTED
    assert isinstance(caught.value.__cause__, CampaignFamilyUnsupported)
    assert str(caught.value.__cause__) == "campaign_family_unsupported"
    assert _tree_bytes(rig.run_root.parent) == after


@pytest.mark.parametrize("family", [12, None])
@pytest.mark.parametrize("progress", [False, True])
def test_the_reader_family_race_keeps_the_approved_cli_refusal(
    tmp_path, monkeypatch, family, progress
):
    import asyncio

    from evolution.tree import evolve as evolve_module
    from tests.campaign_fakes import Rig
    from tests.tree_evolve_support import write_workspace

    rig = Rig(tmp_path / "native")
    asyncio.run(rig.run(crash_after="campaign_created"))
    manifest = write_workspace(tmp_path / "workspace")
    replacement = (old_run_dir(tmp_path, family) / "campaign.jsonl").read_bytes()
    probes, after = _replace_before_read_run_id(monkeypatch, rig.run_root, replacement)

    def unexpected(*args, **kwargs):
        pytest.fail("the family race opened an adapter instead of refusing")

    monkeypatch.setattr(evolve_module, "_http_session_adapter", unexpected)
    result = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(manifest), "--run-dir", str(rig.run_root)]
        + (["--progress"] if progress else []),
    )
    assert probes == [14]
    # This later refusal retains the native pre-admission notes, captured on
    # 1bbc391cb too; only the uncaught exception becomes the approved refusal.
    notes = (
        b"evo tree evolve: note: Corpus split: train (1 cases); held-out test (0 cases); mode implicit_train.\n"
        b"evo tree evolve: note: Corpus partition_mode: implicit_train.\n"
        b"evo tree evolve: note: Corpus split train: 1 cases, 1 groups.\n"
        b'evo tree evolve: note: Corpus split train, stratum "s1": 1 cases, 1 groups.\n'
        b"evo tree evolve: note: Corpus split test: 0 cases, 0 groups.\n"
        b'evo tree evolve: note: Corpus split test, stratum "s1": 0 cases, 0 groups.\n'
        b'evo tree evolve: note: Corpus split coverage: stratum "s1" is absent from test.\n'
    )
    assert (result.exit_code, result.stdout_bytes, result.stderr_bytes) == (
        2,
        b"",
        notes + _evolve_family_stderr(family),
    ), result.exception
    assert _tree_bytes(rig.run_root.parent) == after


@pytest.mark.parametrize(
    ("schema", "with_workflow"),
    [
        ("evolution-v2-manifest/1", True),
        ("evolution-v2-manifest/14", True),
        ("evolution-v2-manifest/15", False),
    ],
)
def test_a_hand_built_manifest_gets_the_current_schema_guidance(tmp_path, schema, with_workflow):
    from evolution.tree.evolve import EvolveRefused, admit_manifest
    from evolution.tree.manifest.manifest_v2 import load_manifest_v2
    from tests.tree_evolve_support import write_workspace

    native = load_manifest_v2(write_workspace(tmp_path))
    manifest = replace(native, schema=schema, workflow=native.workflow if with_workflow else None)
    before = _tree_bytes(tmp_path)
    with pytest.raises(EvolveRefused) as caught:
        admit_manifest(manifest)
    assert caught.value.code == "manifest_schema"
    assert caught.value.path is None
    assert caught.value.expected == "expected evolution-v2-manifest/15 or /16"
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("family", [3, 5, 6, 12, 13, None])
def test_evolve_uses_the_same_guidance_for_every_removed_family(tmp_path, family):
    from evolution.tree.evolve import EvolveRefused, _refuse_unadmitted_family_at

    run = old_run_dir(tmp_path, family)
    with pytest.raises(EvolveRefused) as caught:
        _refuse_unadmitted_family_at(run)
    assert caught.value.code == "campaign_family_unsupported"
    assert caught.value.path == f"family/{'absent' if family is None else family}"
    assert caught.value.expected == REMOVED_FAMILY_EXPECTED


def _finished13(tmp_path):
    """A hand-built historical source envelope, never a relabelled family-14 run.

    Only its family and prefix are needed: a new link must refuse before
    replaying its retired grammar, while an existing link owns its own facts.
    """
    run = old_run_dir(tmp_path, 13)
    created = json.loads((run / "campaign.jsonl").read_text())
    finished = {
        **created,
        "sequence": 2,
        "event": "campaign_finished",
        "payload": {"outcome": "finished", "reason": "rounds_exhausted"},
    }
    with (run / "campaign.jsonl").open("a") as journal:
        journal.write(json.dumps(finished) + "\n")
    return run


@pytest.mark.parametrize("successor_family", [14, 15])
async def test_a_new_run_cannot_link_family_13(tmp_path, successor_family):
    from evolution.tree.campaign import LineageFamilyUnsupported, derive_linked_from
    from evolution.tree.evolve import EvolveRefused, evolve
    from tests.test_tree_evolve import fake_adapters
    from tests.tree_evolve_support import write_workspace

    predecessor = _finished13(tmp_path)
    manifest = write_workspace(tmp_path / "workspace")
    document = json.loads(manifest.read_text())
    if successor_family == 15:
        from tests.family15_fakes import v16_document

        document["schema"] = "evolution-v2-manifest/16"
        document["campaign"]["gate"] = v16_document()["campaign"]["gate"]
    document["linked_from"] = {"run_dir": str(predecessor), "baseline": "fresh"}
    manifest.write_text(json.dumps(document))
    before = _tree_bytes(tmp_path)
    with pytest.raises(LineageFamilyUnsupported) as library:
        derive_linked_from(predecessor, expected_family=successor_family)
    assert library.value.journaled == 13
    with pytest.raises(EvolveRefused) as caught:
        await evolve(manifest, tmp_path / "new-run", adapter_factory=fake_adapters())
    assert (caught.value.code, caught.value.path) == ("lineage_family_unsupported", "linked_from")
    assert caught.value.expected == REMOVED_FAMILY_EXPECTED
    assert _tree_bytes(tmp_path) == before


async def _historically_linked(tmp_path, *, successor_family, import_born):
    """Seal a native current-family prefix around a historical source-13 declaration.

    The source's envelope, the link prefix, and the carried row's origin all
    name the same genuine family-13 reference. Only the successor uses a live
    writer. Measurement identity does not include the campaign's lineage.
    """
    from evolution.tree.baseline_import import BaselineImport, import_pairs_digest
    from evolution.tree.campaign_identity import CampaignIdentityV1, measurement_identity
    from evolution.tree.campaign_measurement import Measurement
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.execution.journal_identity import canonical_json
    from tests.campaign_fakes import Rig
    from tests.family15_fakes import v16_document
    from tests.test_tree_campaign_identity import _linked_from
    from tests.tree.execution.test_journal_durability import imported_payload

    predecessor = _finished13(tmp_path)
    policy = (
        CampaignPolicyV12.from_payload(v16_document()["campaign"])
        if successor_family == 15
        else None
    )
    rig = Rig(tmp_path, campaign_policy=policy)
    await rig.run(crash_after="measurement_bound")
    records = rig.records()
    created = records[0]["payload"]
    assert created["family_version"] == successor_family
    identity = created["identity"]
    candidate = identity["artifact_store"]["seed_candidate_digest"]
    measurement = measurement_identity(
        CampaignIdentityV1(identity), rig.cases, candidate, "champion"
    )
    schedule = measurement.to_payload()["schedule"]
    row = imported_payload(measurement, schedule[0], metrics={rig.policy.metric_primary: 0.0})
    row["source"].update(
        run_id="old-run", candidate_digest=candidate, condition_fields=["identity.policy_digest"]
    )
    declaration = BaselineImport(
        "m-r00-champion",
        "d" * 64,
        import_pairs_digest([row]),
        1,
        len(schedule),
        ("identity.policy_digest",),
        1,
        (row,),
    ).declaration()
    identity["linked_from"] = _linked_from(
        run_id="old-run",
        prefix_digest=hashlib.sha256((predecessor / "campaign.jsonl").read_bytes()).hexdigest(),
        final_champion_digest=candidate,
        final_champion_dependency_digest=identity["artifact_store"]["seed_dependency_digest"],
        predecessor_family=13,
        successor_family=successor_family,
        first_round=1,
        rounds_closed=0,
        alpha_rounds={},
        denominator=0,
        baseline=declaration,
        condition_changed=("identity.policy_digest",),
    ).to_payload()
    created["identity_digest"] = hashlib.sha256(canonical_json(identity)).hexdigest()
    (rig.run_root / "campaign.jsonl").write_bytes(
        b"".join(canonical_json(record) + b"\n" for record in records)
    )
    if import_born:
        bound = next(r["payload"] for r in records if r["event"] == "measurement_bound")
        assert bound["identity_digest"] == measurement.digest
        born = Measurement.declare(
            run_root=rig.run_root,
            campaign_run_id=records[0]["run_id"],
            measurement_id="m-r00-champion",
            identity=measurement,
            corpus=rig.cases,
        )
        born.create(imported=[row])
        born.close()
    return rig, predecessor, declaration


@pytest.mark.parametrize("successor_family", [14, 15])
async def test_a_current_run_linked_to_family_13_still_reads_and_resumes(
    tmp_path, monkeypatch, successor_family
):
    from evolution.tree import campaign
    from evolution.tree.campaign_projections import inspect_view

    rig, predecessor, declaration = await _historically_linked(
        tmp_path, successor_family=successor_family, import_born=True
    )
    assert read_family_version(predecessor) == 13
    assert declaration["imported"] == 1
    before = _tree_bytes(predecessor)

    def unexpected(*args, **kwargs):
        pytest.fail("a completed import reopened its family-13 predecessor")

    # All source replay/row-reading paths are forbidden, even if another entry
    # point were to bypass the normal _predecessor resolver.
    monkeypatch.setattr(campaign, "_predecessor", unexpected)
    monkeypatch.setattr(campaign, "read_source", unexpected)
    path_open = Path.open

    def own_files_only(path, *args, **kwargs):
        if path == predecessor or predecessor in path.parents:
            pytest.fail("a completed-import reader opened the predecessor directly")
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", own_files_only)
    for finished in (False, True):
        own_before = _tree_bytes(rig.run_root)
        document = inspect_campaign(rig.run_root, verify=True).to_payload()
        assert document["verification"]["mismatches"] == []
        assert (inspect_view(rig.run_root).snapshot.finished is not None) == finished
        assert open_run(rig.run_root).snapshot.run_id == read_run_id(rig.run_root)
        for argv in (
            ["watch", "--run-dir", str(rig.run_root), "--once"],
            ["trace", "--run-dir", str(rig.run_root), "--out", "-"],
        ):
            result = CliRunner().invoke(tree_app, argv)
            assert result.exit_code == 0, (result.stderr, result.exception)
        assert _tree_bytes(rig.run_root) == own_before
        result = await rig.resume(linked_from=predecessor)
        assert result.finished is not None
        if finished:
            assert _tree_bytes(rig.run_root) == own_before
    monkeypatch.setattr(Path, "open", path_open)
    assert _tree_bytes(predecessor) == before


@pytest.mark.parametrize("successor_family", [14, 15])
async def test_an_owed_baseline_import_from_family_13_reports_baseline_import_unavailable(
    tmp_path, monkeypatch, successor_family
):
    from evolution.tree import campaign
    from evolution.tree.campaign import BaselineImportUnavailable, LineageFamilyUnsupported
    from evolution.tree.execution.authorizations import baseline_import_instance_digest

    rig, predecessor, declared = await _historically_linked(
        tmp_path, successor_family=successor_family, import_born=False
    )
    before = _tree_bytes(tmp_path)
    original = campaign._predecessor
    seen = []
    refused = []

    def reread(root, **kwargs):
        seen.append((root, read_family_version(root)))
        try:
            return original(root, **kwargs)
        except LineageFamilyUnsupported as exc:
            refused.append(exc.journaled)
            raise

    monkeypatch.setattr(campaign, "_predecessor", reread)
    with pytest.raises(BaselineImportUnavailable) as caught:
        await rig.resume(linked_from=predecessor)
    assert seen == [(predecessor, 13)]
    assert refused == [13]
    assert caught.value.instance_digest == baseline_import_instance_digest(
        rig.records()[0]["run_id"], declared["pairs_digest"]
    )
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("family", [14, 15])
def test_current_creation_grammars_keep_recorded_family_13_predecessors(family):
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.campaign_schema import campaign_family_for
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.storage.envelope import Envelope
    from tests.family15_fakes import family15_created, v16_document
    from tests.test_tree_campaign_family14_reports import _linked_created

    created = _linked_created(family=13)
    if family == 15:
        policy = CampaignPolicyV12.from_payload(v16_document()["campaign"])
        native = family15_created(policy)
        native["identity"]["linked_from"] = created["identity"]["linked_from"]
        created = native
        created["identity_digest"] = hashlib.sha256(canonical_json(created["identity"])).hexdigest()
    envelope = Envelope.from_payload(
        {
            "schema_version": 1,
            "run_id": "successor",
            "sequence": 1,
            "timestamp": "2026-10-20T00:00:00Z",
            "event": "campaign_created",
            "operation_id": None,
            "payload": created,
        },
        family=campaign_family_for(family),
    )
    state = CampaignLifecycle(selector=None, family_version=family)
    state.accept(envelope)
    assert state.snapshot.identity["linked_from"]["predecessor_family"] == 13


@pytest.mark.parametrize("family", [6, 12, 13])
def test_the_owed_terminal_probe_never_replays_a_removed_family(tmp_path, monkeypatch, family):
    from evolution.tree import campaign

    run = old_run_dir(tmp_path, family)

    def unexpected(*args, **kwargs):
        pytest.fail("the owed-terminal probe replayed a removed grammar")

    monkeypatch.setattr(campaign, "inspect_log", unexpected)
    assert campaign.owes_applied_terminal(run) is False
