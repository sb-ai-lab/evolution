"""Secret oracle paths in real /16 campaigns and their recovery."""

import json

import pytest

from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.authorizations import comparable_field
from tests.campaign_drivers import audit_mismatches
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family14_visibility_change import _decision
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

pytestmark = pytest.mark.slow

ORACLE = {"critical": True, "reference": "GOLD-FACT-4242", "reference_spec": {"field": "unit_name"}}


def declaring(pointers):
    def configure(document):
        if pointers is not None:
            document["secret_oracle_paths"] = {
                "version": "secret_oracle_paths/1",
                "pointers": list(pointers),
            }

    return configure


async def test_a_changed_declaration_is_a_rebase_that_keeps_the_open_round(tmp_path, monkeypatch):
    options = {"extra_cases": {"c3": ORACLE}}
    with pytest.raises(PowerCut):
        await public_run(tmp_path, monkeypatch, cut="after_attempt_input_frozen", **options)
    run = tmp_path / "run"
    cut = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused) as caught:
        await public_run(tmp_path, monkeypatch, configure=declaring(["/reference"]), **options)
    refused = caught.value
    assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
    assert [d.field for d in refused.differences] == ["identity.secret_oracle_paths"]
    assert (run / "campaign.jsonl").read_bytes() == cut
    decision = _decision(refused, None, reason="accept oracle declaration")
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=declaring(["/reference"]),
        authorizations=[decision],
        **options,
    )
    assert result.terminal == "finished"
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["changed_fields"] == ["identity.secret_oracle_paths"]
    (frozen,) = payloads(run, "attempt_input_frozen")
    assert payloads(run, "editor_started")[0]["attempt_id"] == frozen["attempt_id"]
    assert len(payloads(run, "round_closed")) == 1
    assert audit_mismatches(run) == []


def test_the_secret_oracle_declaration_is_not_a_comparable_field():
    assert comparable_field("identity.secret_oracle_paths", family_version=15) is False


async def test_a_v16_campaign_with_verbatim_answers_alone_binds_and_resumes(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_verbatim_answers import CANNED, quoting_reflector

    def configure(document):
        document["verbatim_answers"] = {"version": "verbatim_answers/1", "strings": [CANNED]}

    options = dict(
        configure=configure,
        extra_cases={"c3": {"critical": True, "answer": CANNED}},
        reflector=quoting_reflector(CANNED),
    )
    with pytest.raises(PowerCut):
        await public_run(tmp_path, monkeypatch, cut="after_attempt_input_frozen", **options)
    run, result = await public_run(tmp_path, monkeypatch, **options)
    (created,) = payloads(run, "campaign_created")
    assert created["family_version"] == 15 and "verbatim_answers" in created["identity"]
    assert "secret_oracle_paths" not in created["identity"]
    assert result.terminal == "finished" and not payloads(run, "identity_rebased")
    assert payloads(run, "reflection_finished") and not payloads(run, "reflection_failed")
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("declared", [True, False])
async def test_a_reflector_naming_unit_name(tmp_path, monkeypatch, declared):
    from tests.test_tree_campaign_family14_verbatim_answers import quoting_reflector

    run, _ = await public_run(
        tmp_path,
        monkeypatch,
        configure=declaring(["/reference"] if declared else None),
        extra_cases={"c3": ORACLE},
        reflector=quoting_reflector("unit_name"),
    )
    if declared:
        assert payloads(run, "reflection_finished") and not payloads(run, "reflection_failed")
    else:
        assert payloads(run, "reflection_failed")[0]["error_class"] == "oracle_leak"


@pytest.mark.parametrize("declared", [True, False])
async def test_a_candidate_writing_unit_name_at_the_seal(tmp_path, monkeypatch, declared):
    from tests.test_tree_campaign_family14_verbatim_answers import canned_editor

    canned_editor(monkeypatch, tmp_path, text="unit_name")
    run, _ = await public_run(
        tmp_path,
        monkeypatch,
        configure=declaring(["/reference"] if declared else None),
        extra_cases={"c3": ORACLE},
    )
    if declared:
        assert payloads(run, "attempt_sealed")
    else:
        assert [(p["reason"], p["code"]) for p in payloads(run, "attempt_closed")] == [
            ("seal_refused", "oracle_leak")
        ]


@pytest.mark.parametrize("declared", [True, False])
async def test_attempt_feedback_carrying_unit_name(tmp_path, monkeypatch, declared):
    from tests.test_tree_campaign_family14_verbatim_answers import canned_editor

    canned_editor(monkeypatch, tmp_path, text="unit_name")
    run, _ = await public_run(
        tmp_path,
        monkeypatch,
        configure=declaring(["/reference"] if declared else None),
        extra_cases={"c3": ORACLE},
        campaign={
            "attempts_per_round": 2,
            "attempt_feedback": "sequential",
            "round_stop": "all_attempts",
        },
    )
    frozen = payloads(run, "attempt_input_frozen")
    (row,) = frozen[1]["feedback"]["attempts"]
    assert ("unit_name" in json.dumps(row)) is declared
    assert row["changes_status"] == ("available" if declared else "unavailable")


@pytest.mark.parametrize("declared", [True, False])
async def test_a_model_grouper_description_naming_unit_name(tmp_path, monkeypatch, declared):
    from tests.model_grouping_fakes import PortionGrouper, chat_grouper_section, use_chat_grouper

    use_chat_grouper(monkeypatch, PortionGrouper(title="unit_name"))

    def configure(document):
        declaring(["/reference"] if declared else None)(document)
        document["grouper"] = chat_grouper_section()

    run, _ = await public_run(
        tmp_path, monkeypatch, configure=configure, extra_cases={"c3": ORACLE}
    )
    finished = payloads(run, "grouping_finished")
    assert finished
    assert ("unit_name" in json.dumps(finished)) is declared


async def test_an_empty_pointer_list_admits_oracle_strings_but_not_run_secrets(
    tmp_path, monkeypatch
):
    from tests.test_tree_campaign_family14_verbatim_answers import quoting_reflector

    for name, text in (("oracle", "GOLD-FACT-4242"), ("secret", "forwarded-b12-secret-value")):
        monkeypatch.setenv("B12_TOKEN", "forwarded-b12-secret-value")

        def configure(document):
            declaring([])(document)
            document["editor"]["allowed_env"] = ["B12_TOKEN"]

        run, _ = await public_run(
            tmp_path / name,
            monkeypatch,
            configure=configure,
            extra_cases={"c3": ORACLE},
            reflector=quoting_reflector(text),
        )
        if name == "oracle":
            assert payloads(run, "reflection_finished")
        else:
            assert payloads(run, "reflection_failed")[0]["error_class"] == "invalid_result"


async def test_a_reflection_leak_names_the_case_and_pointer(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_verbatim_answers import quoting_reflector

    run, _ = await public_run(
        tmp_path,
        monkeypatch,
        extra_cases={"c3": ORACLE},
        reflector=quoting_reflector("GOLD-FACT-4242"),
    )
    (failed,) = payloads(run, "reflection_failed")
    assert failed["leak"] == {"case_id": "c3", "oracle_pointer": "/reference"}


async def test_a_seal_leak_names_the_case_and_pointer(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_verbatim_answers import canned_editor

    canned_editor(monkeypatch, tmp_path, text="GOLD-FACT-4242")
    run, _ = await public_run(tmp_path, monkeypatch, extra_cases={"c3": ORACLE})
    (closed,) = payloads(run, "attempt_closed")
    assert closed["code"] == "oracle_leak" and closed["leak"] == {
        "case_id": "c3",
        "oracle_pointer": "/reference",
    }


async def test_a_grouping_leak_names_the_case_and_pointer(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_grouper_run import GROUP_ALL_PROGRAM, _command_grouper

    # The command runs a sealed real helper; only its output label is synthetic.
    tmp_path.mkdir(exist_ok=True)
    grouper = _command_grouper(
        tmp_path, GROUP_ALL_PROGRAM.replace("every failure", "GOLD-FACT-4242")
    )

    def configure(document):
        document["grouper"] = grouper

    run, _ = await public_run(
        tmp_path, monkeypatch, configure=configure, extra_cases={"c3": ORACLE}
    )
    (failed,) = payloads(run, "grouping_failed")
    assert failed["code"] == "oracle_leak" and failed["leak"] == {
        "case_id": "c3",
        "oracle_pointer": "/reference",
    }

    assert audit_mismatches(run) == []


@pytest.mark.parametrize(
    "key",
    ["forwarded-b12-secret-value", "k" * 1100, "я" * 512],
    ids=["run-secret", "ascii-overflow", "utf8-overflow"],
)
async def test_a_secret_or_oversized_leak_pointer_is_omitted_and_the_failure_is_still_written(
    tmp_path, monkeypatch, key
):
    from tests.test_tree_campaign_family14_verbatim_answers import quoting_reflector

    monkeypatch.setenv("B12_TOKEN", "forwarded-b12-secret-value")

    def configure(document):
        document["editor"]["allowed_env"] = ["B12_TOKEN"]

    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        extra_cases={"c3": {"critical": True, key: "GOLD-FACT-4242"}},
        reflector=quoting_reflector("GOLD-FACT-4242"),
    )
    assert payloads(run, "reflection_failed")[0]["leak"] == {"case_id": "c3"}
    assert result.terminal == "finished" and audit_mismatches(run) == []


async def test_the_leak_location_never_reaches_a_role_input(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_verbatim_answers import canned_editor

    canned_editor(monkeypatch, tmp_path, text="GOLD-FACT-4242")
    run, _ = await public_run(
        tmp_path, monkeypatch, extra_cases={"c3": ORACLE}, campaign={"rounds": 2}
    )
    assert len(payloads(run, "reflection_finished")) == 2
    for path in run.rglob("*"):
        if path.is_file() and path.name != "campaign.jsonl":
            assert (
                b"oracle_pointer" not in path.read_bytes()
                and b"/reference" not in path.read_bytes()
            )


@pytest.mark.parametrize("cut", ["before_refusal", "after_refusal"])
async def test_a_leak_record_replays_audits_and_verifies(tmp_path, monkeypatch, cut):
    from evolution.tree.campaign import _Campaign, inspect_campaign
    from tests.test_tree_campaign_family14_verbatim_answers import canned_editor

    canned_editor(monkeypatch, tmp_path, text="GOLD-FACT-4242")
    with monkeypatch.context() as patch:
        if cut == "before_refusal":
            patch.setattr(_Campaign, "candidate_leak", lambda self, attempt: None)
            from evolution.tree.artifacts.artifacts import ArtifactStore

            original_hook = ArtifactStore._hook

            def cut_store(self, name):
                original_hook(self, name)
                if name == "after_seal_finished":
                    raise PowerCut()

            patch.setattr(ArtifactStore, "_hook", cut_store)
            with pytest.raises(PowerCut):
                await public_run(tmp_path, patch, extra_cases={"c3": ORACLE})
        else:
            original = _Campaign.append_seal_refusal

            def interrupted(self, *args, **kwargs):
                original(self, *args, **kwargs)
                raise PowerCut()

            patch.setattr(_Campaign, "append_seal_refusal", interrupted)
            with pytest.raises(PowerCut):
                await public_run(tmp_path, patch, extra_cases={"c3": ORACLE})
    run, _ = await public_run(tmp_path, monkeypatch, extra_cases={"c3": ORACLE})
    assert payloads(run, "attempt_closed")[0]["leak"] == {
        "case_id": "c3",
        "oracle_pointer": "/reference",
    }
    assert audit_mismatches(run) == [] and inspect_campaign(run, verify=True) is not None


async def test_family_14_runs_never_write_leak(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_run import family14_run
    from tests.test_tree_campaign_family14_verbatim_answers import quoting_reflector

    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        extra_cases={"c3": ORACLE},
        reflector=quoting_reflector("GOLD-FACT-4242"),
    )
    assert "leak" not in payloads(run, "reflection_failed")[0]
