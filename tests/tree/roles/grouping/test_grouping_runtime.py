"""Runtime assembly around the pure adaptive grouping core."""

from __future__ import annotations

from evolution.tree.campaign_identity import build_adaptation_identity
from evolution.tree.execution.command_binding import CommandBinding
from evolution.tree.roles.grouping.grouping_runtime import (
    AdaptiveCampaignRuntimeConfig,
    GroupingRuntime,
    SelectionRuntimeConfig,
)
from evolution.tree.roles.grouping.reflect_grouping import (
    FieldFeature,
    GroupingRecord,
    build_registry,
)
from evolution.tree.roles.reflection_reader import FrozenReflectionView


def _config(
    *, context_page_size: int = 32, families: dict[str, str] | None = None
) -> AdaptiveCampaignRuntimeConfig:
    identity = build_adaptation_identity(
        grouping={
            "kind": "fields",
            "features": [
                {
                    "name": "terminal",
                    "source": "observation",
                    "path": "/terminal",
                    "description": "terminal result",
                }
            ],
        },
        selection={
            "focus_cases": 16,
            "representatives_per_case": 2,
            "context_page_size": context_page_size,
        },
        families=families or {"kind": "none"},
        attempt_feedback="none",
        reflection_metadata_digest="a" * 64,
        environment_docs_digest=None,
    )
    return AdaptiveCampaignRuntimeConfig(
        identity=identity,
        grouping_features=(
            FieldFeature("terminal", "observation", "/terminal", "terminal result"),
        ),
        families=families or {"kind": "none"},
        selection=SelectionRuntimeConfig(context_page_size=context_page_size),
    )


def test_builtin_runtime_groups_only_failing_records_by_configured_features() -> None:
    runtime = GroupingRuntime(_config())

    def sources(terminal: str) -> dict[str, object]:
        return {
            "case": {},
            "public": {},
            "observation": {"terminal": terminal},
            "feedback": {},
        }

    registry = build_registry(
        "b" * 64,
        (
            GroupingRecord("obs:a:1", "a", 1, True, True, sources("timeout")),
            GroupingRecord("obs:b:1", "b", 1, True, True, sources("timeout")),
            GroupingRecord("obs:c:1", "c", 1, False, True, sources("ok")),
        ),
    )

    grouping = runtime.builtin(registry)

    assert len(grouping.neighborhoods) == 1
    assert grouping.neighborhoods[0].member_ids == ("obs:a:1", "obs:b:1")
    assert grouping.result.assignments[2] is None


def test_runtime_default_selection_and_metadata_are_frozen() -> None:
    config = _config()

    assert config.selection.focus_cases == 16
    assert config.selection.representatives_per_case == 2
    assert config.selection.context_page_size == 32
    assert config.reflection_metadata == {}
    assert config.policy_digest != config.identity.digest


def test_grouping_identity_excludes_later_selection_and_family_policy() -> None:
    base = _config()
    changed = _config(context_page_size=64, families={"kind": "metadata", "path": "/family"})

    registry = build_registry(
        "b" * 64,
        (
            GroupingRecord(
                "obs:a:1",
                "a",
                1,
                True,
                True,
                {"case": {}, "public": {}, "observation": {"terminal": "timeout"}, "feedback": {}},
            ),
            GroupingRecord(
                "obs:b:1",
                "b",
                1,
                True,
                True,
                {"case": {}, "public": {}, "observation": {"terminal": "timeout"}, "feedback": {}},
            ),
        ),
    )

    assert base.policy_digest == changed.policy_digest
    assert (
        GroupingRuntime(base).builtin(registry).neighborhoods[0].group_id
        == GroupingRuntime(changed).builtin(registry).neighborhoods[0].group_id
    )


async def test_command_request_version_is_two_only_under_scorer_diagnostics(tmp_path) -> None:
    """Family 8 only (ADR-0021): the sealed request's `version` widens to
    `reflect_grouping_request/2` only when the caller says the bound
    baseline's inputs admit `diagnostics`; families 5-7 keep `/1` byte for
    byte (the default)."""

    program = tmp_path / "group.py"
    program.write_text(
        "import json, pathlib, sys\n"
        "request = json.load(open(sys.argv[-1]))\n"
        "pathlib.Path('version.txt').write_text(request['version'])\n"
        "print(json.dumps({'version':'reflect_grouping_result/1',"
        "'registry_digest':request['registry_digest'],'assignments':[0,0]}))\n",
        encoding="utf-8",
    )
    command = CommandBinding(("python3", str(program)), tmp_path)
    identity = build_adaptation_identity(
        grouping={"kind": "command", "command_digest": "c" * 64, "config_digest": "d" * 64},
        selection={"focus_cases": 16, "representatives_per_case": 2, "context_page_size": 32},
        families={"kind": "none"},
        attempt_feedback="none",
        reflection_metadata_digest="a" * 64,
        environment_docs_digest=None,
    )
    runtime = GroupingRuntime(
        AdaptiveCampaignRuntimeConfig(identity=identity, custom_command=command)
    )
    registry = build_registry(
        "b" * 64,
        tuple(
            GroupingRecord(
                f"obs:{case}:1",
                case,
                1,
                True,
                True,
                {"case": {}, "public": {}, "observation": {}, "feedback": {}},
            )
            for case in ("a", "b")
        ),
    )
    reader = FrozenReflectionView(
        {"list_grouping_inputs": lambda _args: {"entries": []}}
    ).open_operation("grouping-test", {"list_grouping_inputs"})

    await runtime.command(registry, reader=reader, control_root=tmp_path / "control-default")
    assert (tmp_path / "version.txt").read_text() == "reflect_grouping_request/1"

    reader = FrozenReflectionView(
        {"list_grouping_inputs": lambda _args: {"entries": []}}
    ).open_operation("grouping-test-2", {"list_grouping_inputs"})
    await runtime.command(
        registry,
        reader=reader,
        control_root=tmp_path / "control-diagnostics",
        scorer_diagnostics=True,
    )
    assert (tmp_path / "version.txt").read_text() == "reflect_grouping_request/2"


async def test_command_runtime_admits_a_child_partition_over_the_bound_registry(tmp_path) -> None:
    program = tmp_path / "group.py"
    program.write_text(
        "import json, sys\nrequest = json.load(open(sys.argv[-1]))\n"
        "print(json.dumps({'version':'reflect_grouping_result/1','registry_digest':request['registry_digest'],'assignments':[0,0]}))\n",
        encoding="utf-8",
    )
    command = CommandBinding(("python3", str(program)), tmp_path)
    identity = build_adaptation_identity(
        grouping={"kind": "command", "command_digest": "c" * 64, "config_digest": "d" * 64},
        selection={"focus_cases": 16, "representatives_per_case": 2, "context_page_size": 32},
        families={"kind": "none"},
        attempt_feedback="none",
        reflection_metadata_digest="a" * 64,
        environment_docs_digest=None,
    )
    runtime = GroupingRuntime(
        AdaptiveCampaignRuntimeConfig(identity=identity, custom_command=command)
    )
    registry = build_registry(
        "b" * 64,
        tuple(
            GroupingRecord(
                f"obs:{case}:1",
                case,
                1,
                True,
                True,
                {"case": {}, "public": {}, "observation": {}, "feedback": {}},
            )
            for case in ("a", "b")
        ),
    )
    reader = FrozenReflectionView(
        {"list_grouping_inputs": lambda _args: {"entries": []}}
    ).open_operation("grouping-test", {"list_grouping_inputs"})

    grouping = await runtime.command(registry, reader=reader, control_root=tmp_path / "control")

    assert grouping.neighborhoods[0].member_ids == ("obs:a:1", "obs:b:1")


def test_family_pointer_reuses_typed_pointer_for_frozen_arrays():
    from dataclasses import replace

    from tests.campaign_fakes import corpus

    config = replace(_config(), reflection_metadata={"case-01": {"a/b": {"~items": ["family"]}}})
    for pointer, expected in [
        ("/a~1b/~0items/0", "family"),
        ("/a~1b/~0items/²", None),
        ("/a~1b/~0items/٢", None),
        ("/a~1b/~0items/999999999999999999999999999", None),
    ]:
        changed = replace(config, families={"kind": "metadata", "path": pointer})
        assert changed.family_membership(corpus(1)) == {"case-01": expected}
        assert changed.policy_digest == config.policy_digest


def test_family15_builtin_descriptions_and_screens():
    runtime = GroupingRuntime(_config())
    registry = build_registry(
        "b" * 64,
        [
            GroupingRecord(
                "obs:a:1",
                "a",
                1,
                True,
                True,
                {"case": {}, "public": {}, "observation": {"terminal": "timeout"}, "feedback": {}},
            )
        ],
    )
    assert runtime.builtin(registry).neighborhoods[0].description is None
    assert (
        runtime.builtin(registry, family15=True).neighborhoods[0].description.label
        == 'terminal="timeout"'
    )
    assert (
        runtime.builtin(
            registry,
            family15=True,
            forbidden_string_predicates=(("oracle_leak", lambda text: "timeout" in text),),
        )
        .neighborhoods[0]
        .description
        is None
    )
