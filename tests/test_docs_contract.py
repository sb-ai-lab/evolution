"""Contract checks for the live documentation surface.

The checks use only the Python standard library so they run in the core and
experiment-free CI jobs.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).parents[1]
CORE_DOCS = (
    "AGENTS.md",
    "README.md",
    "README.ru.md",
    "QUICKSTART.md",
    "QUICKSTART.ru.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "docs/SUMMARY.md",
    "docs/after-bench-import.md",
    "docs/api-usage.md",
    "docs/architecture.md",
    "docs/cli.md",
    "docs/configuration.md",
    "docs/custom-tasks.md",
    "docs/manifest-v2.md",
    "docs/manual-workflow.md",
    "docs/manual-architecture.md",
    "docs/running-campaigns.md",
    "docs/connecting-an-agent.md",
    "docs/reference-target.md",
    "docs/run-storage-layout.md",
    "docs/splits.md",
    "docs/target-server.md",
    "docs/evolving-skills/SKILL.md",
    "docs/evolving-skills/reference.md",
    "docs/evolving-skills/trace-analysis.md",
    "docs/child-process-environment.md",
    "docs/prompts_examples.md",
)
LOCALIZED_DOCS = frozenset({"README.ru.md", "QUICKSTART.ru.md"})
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
STRUCTURAL = re.compile(r"^\s*(?:$|#{1,6}\s|[-+*]\s|\d+[.)]\s|>|\||<!--|</?|\[[^]]+\]:|---$)")
FORBIDDEN_FLAT_CLI = re.compile(
    r"\bevo (?:bench|commit|containers?|diff|discard|history|import|lineage|list|promote|"
    r"reflect|repair|rewrite|show|show-trace|solve|split-check|split-list|status|traces|validate)\b"
)
CLASSIFICATION_PATH = ROOT / "docs" / "markdown-classification.json"
MARKDOWN_CLASSES = {
    "maintained_documentation",
    "dual_use_documentation_and_executable_instruction",
    "runtime_prompt_markdown",
    "test_fixture",
    "historical_or_frozen_research_record",
}


def _available_docs() -> tuple[str, ...]:
    return CORE_DOCS


def _tracked_files() -> set[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return {item.decode() for item in output.split(b"\0") if item}


def _owned_markdown(tracked: set[str]) -> set[str]:
    # Direct child distributions maintain their own documentation inventory.
    distributions = {
        path.removesuffix("pyproject.toml")
        for path in tracked
        if path.count("/") == 1 and path.endswith("/pyproject.toml")
    }
    return {
        path
        for path in tracked
        if path.endswith(".md") and not any(path.startswith(root) for root in distributions)
    }


def _markdown_classifications() -> dict[str, str]:
    value = json.loads(CLASSIFICATION_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _headings(path: Path) -> set[str]:
    headings: set[str] = set()
    duplicates: dict[str, int] = {}
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if not match:
            continue
        text = re.sub(r"<[^>]+>|[`*_~]", "", match.group(1)).strip().lower()
        slug = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
        slug = re.sub(r"[\s-]+", "-", slug).strip("-")
        count = duplicates.get(slug, 0)
        duplicates[slug] = count + 1
        headings.add(slug if count == 0 else f"{slug}-{count}")
    return headings


@pytest.mark.parametrize("relative", CORE_DOCS)
def test_live_document_manifest_is_explicit_and_tracked(relative: str) -> None:
    assert relative in _tracked_files()
    assert (ROOT / relative).is_file()


def test_every_tracked_markdown_file_has_one_classification() -> None:
    tracked_markdown = _owned_markdown(_tracked_files())
    classifications = _markdown_classifications()

    assert set(classifications) == tracked_markdown
    assert set(classifications.values()) <= MARKDOWN_CLASSES
    assert all(isinstance(kind, str) and kind for kind in classifications.values())


def test_canonical_documentation_is_classified_as_maintained() -> None:
    classifications = _markdown_classifications()
    for relative in (
        "README.md",
        "QUICKSTART.md",
        "docs/cli.md",
        "docs/api-usage.md",
        "docs/configuration.md",
        "docs/architecture.md",
        "docs/run-storage-layout.md",
    ):
        assert classifications[relative] == "maintained_documentation"


@pytest.mark.parametrize("relative", CORE_DOCS)
def test_live_prose_is_english_and_paragraphs_are_single_line(relative: str) -> None:
    if _markdown_classifications()[relative] not in {
        "maintained_documentation",
        "dual_use_documentation_and_executable_instruction",
    }:
        return
    path = ROOT / relative
    lines = path.read_text(encoding="utf-8").splitlines()
    fenced = False
    frontmatter = relative.endswith("/SKILL.md") and lines[:1] == ["---"]
    previous_prose = False
    for number, line in enumerate(lines, 1):
        if number > 1 and frontmatter and line == "---":
            frontmatter = False
            previous_prose = False
            continue
        if frontmatter:
            continue
        if FENCE.match(line):
            fenced = not fenced
            previous_prose = False
            continue
        if fenced:
            continue
        if relative not in LOCALIZED_DOCS and not re.match(r"^(?: {4}|\t)", line):
            prose_without_code = re.sub(r"`[^`]*`", "", line)
            assert not CYRILLIC.search(prose_without_code), (
                f"{relative}:{number}: non-English prose"
            )
        list_item = bool(re.match(r"^\s*(?:[-+*]|\d+[.)])\s+\S", line))
        quoted_prose = bool(re.match(r"^\s*>\s+(?!\[!)\S", line))
        prose = list_item or quoted_prose or (bool(line.strip()) and not STRUCTURAL.match(line))
        continuation = prose and previous_prose and not list_item
        assert not continuation, f"{relative}:{number}: wrapped prose paragraph"
        previous_prose = prose and not line.endswith("  ")


def assert_relative_links(relative_paths: tuple[str, ...]) -> None:
    tracked = _tracked_files()
    for relative in relative_paths:
        source = ROOT / relative
        fenced = False
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            for raw in LINK.findall(line):
                target = raw.split(maxsplit=1)[0].strip("<>")
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                file_part, _, anchor = target.partition("#")
                destination = (
                    source if not file_part else (source.parent / unquote(file_part)).resolve()
                )
                destination_relative = destination.relative_to(ROOT).as_posix()
                assert destination_relative in tracked, (
                    f"{relative}:{number}: untracked link {target}"
                )
                assert destination.is_file(), f"{relative}:{number}: missing link {target}"
                if anchor:
                    assert unquote(anchor).lower() in _headings(destination), (
                        f"{relative}:{number}: missing anchor {target}"
                    )


def test_relative_links_are_tracked_and_anchors_exist() -> None:
    assert_relative_links(_available_docs())


def test_documented_cli_uses_the_current_grouped_surface() -> None:
    text = "\n".join((ROOT / path).read_text(encoding="utf-8") for path in _available_docs())
    assert not FORBIDDEN_FLAT_CLI.search(text)
    assert "--agent-permission" not in text
    assert "evo task import " not in text
    for command in (
        "evo run solve",
        "evo task import-sb-bench",
        "evo skill import-skillsbench",
        "evo split check",
        "evo split list",
        "evo tree evolve",
        "evo tree conformance",
        "evo tree authorize",
        "--permission unsafe",
    ):
        assert command in text


def test_edit_brake_contract_is_documented() -> None:
    cli = (ROOT / "docs/cli.md").read_text(encoding="utf-8")
    config = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    summary = (ROOT / "docs/SUMMARY.md").read_text(encoding="utf-8")

    assert "--max-rewrite-attempts" in cli
    assert "--no-version-decay" in cli
    assert "EVO_REWRITE_MAX_DELTA_FRAC" in config
    assert "EVO_C1_EDIT_BUDGET_FRAC" in config
    assert "manual-workflow.md" in summary


def test_documented_install_extras_exist() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = set(project["optional-dependencies"])
    assert extras == {"gigachat", "llm", "tree"}
    for relative in _available_docs():
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "pip install evolution[" not in text
        assert ".[dev" not in text
        for group in re.findall(r"evo-skills\[([^]]+)\]", text):
            assert set(group.split(",")) <= extras, f"{relative}: invalid extras {group}"


def test_quickstart_ids_and_citation_are_current() -> None:
    quickstart = (ROOT / "QUICKSTART.md").read_text(encoding="utf-8")
    assert "hello-evolution" in quickstart and "hello-skill" in quickstart
    assert "demo-task" not in quickstart and "demo-skill" not in quickstart
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    version = re.search(
        r'^__version__ = "([^"]+)"', (ROOT / "src/evolution/__init__.py").read_text(), re.M
    )
    assert version is not None and f'version: "{version.group(1)}"' in citation
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert f'repository-code: "{project["urls"]["Repository"]}"' in citation


def test_reference_target_page_embeds_the_scaffold_manifest() -> None:
    from evolution.tree.reference_target_cli import render_sample_manifest

    text = (ROOT / "docs" / "reference-target.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, flags=re.S)
    assert render_sample_manifest() in blocks
    assert "evo-target init" in text and "evo-target serve" in text


def test_reference_target_page_embeds_the_sample_programs() -> None:
    from evolution.tree.reference_target_cli import sample_program_text

    text = (ROOT / "docs" / "reference-target.md").read_text(encoding="utf-8")
    python_blocks = re.findall(r"```python\n(.*?)```", text, flags=re.S)
    assert sample_program_text("score.py") in python_blocks
    assert sample_program_text("edit.py") in python_blocks
    assert sample_program_text("reflect.py") in python_blocks
    jsonl_blocks = re.findall(r"```jsonl\n(.*?)```", text, flags=re.S)
    assert sample_program_text("corpus.jsonl") in jsonl_blocks
    assert "evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-1" in text
    assert "evo target " not in text


def test_manifest_v2_opening_sample_is_the_scaffold_rendering() -> None:
    from evolution.tree.reference_target_cli import render_sample_manifest

    text = (ROOT / "docs" / "manifest-v2.md").read_text(encoding="utf-8")
    first = re.search(r"```json\n(.*?)```", text, flags=re.S)
    assert first is not None and first.group(1) == render_sample_manifest()


def test_cli_page_names_the_scaffold_schema_the_code_writes() -> None:
    """The CLI page's one claim about the scaffold's schema, pinned to `SAMPLE_MANIFEST`.

    The sentence said `/2` for a whole schema advance because nothing compared
    it with the document `evo-target init` actually writes. It now names
    exactly the version the scaffold renders and no other.
    """

    from evolution.tree.reference_target_cli import render_sample_manifest

    version = json.loads(render_sample_manifest())["schema"].rsplit("/", 1)[-1]
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    (sentence,) = [
        line for line in text.splitlines() if line.startswith("The scaffolded manifest is ")
    ]
    assert set(re.findall(r"`/(\d+)`", sentence)) == {version}, sentence


def test_cli_page_no_longer_asks_for_hand_written_sections() -> None:
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert "Add the five workflow sections" not in text
    assert "evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-1" in text


def test_manifest_v2_documents_every_editor_kind_and_key() -> None:
    import dataclasses

    from evolution.tree.roles.config import AgentCliEditorSection, ChatEditorSection

    text = (ROOT / "docs" / "manifest-v2.md").read_text(encoding="utf-8")
    assert '"kind": "chat"' in text
    assert '"kind": "agent_cli"' in text

    budget_keys = ("max_changed_files", "max_written_bytes", "max_payload_bytes")
    for section in (ChatEditorSection, AgentCliEditorSection):
        for field in dataclasses.fields(section):
            if field.name == "budgets":
                for key in budget_keys:
                    assert f"`{key}`" in text, f"{section.__name__}: missing `{key}`"
                continue
            assert f"`{field.name}`" in text, f"{section.__name__}: missing `{field.name}`"

    for code in ("editor_env_missing", "editor_program_missing", "editor_invalid"):
        assert f"`{code}`" in text, f"missing refusal code `{code}`"


def test_manifest_doc_names_the_c1_identity_refusals() -> None:
    text = (ROOT / "docs" / "manifest-v2.md").read_text(encoding="utf-8")
    for code in ("corpus_too_large", "record_matches_secret", "campaign_family_unsupported"):
        assert f"`{code}`" in text, f"missing refusal code `{code}`"
    for token in ("`family/absent`", "`record-key`", "`envelope.`"):
        assert token in text


def test_retired_editor_environment_names_are_gone() -> None:
    retired = (
        "EVO_EDITOR_MODEL",
        "EVO_EDITOR_BASE_URL",
        "EVO_EDITOR_API_KEY",
        "EVO_EDITOR_PROXY",
        "EVO_EDITOR_PROVIDER_ONLY",
        "EVO_EDITOR_QUANTIZATIONS",
        "EVO_EDITOR_REASONING_EFFORT",
        "EVO_TREE_EDITOR_TIMEOUT_S",
    )
    for path in (ROOT / "src").rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name in retired:
            assert name not in text, f"{path.relative_to(ROOT)}: retired name {name} still present"


def test_manifest_doc_names_the_run_dir_parent_refusal() -> None:
    text = (ROOT / "docs" / "manifest-v2.md").read_text(encoding="utf-8")
    assert "`run_dir_parent_missing`" in text
    assert "`run_dir_parent_missing`" in (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")


#: A manifest schema named in prose, in either spelling the pages use:
#: `` `evolution-v2-manifest/7` `` in full, or the bare `` `/7` `` shorthand.
#: Deliberately narrow, so `` `edit_brief/4` `` and `` `conformance/v2` `` are
#: not schema lists.
SCHEMA_TOKEN = re.compile(r"`(?:evolution-v2-manifest)?/(\d+)`")
FULL_SCHEMA_TOKEN = re.compile(r"`evolution-v2-manifest/(\d+)`")
SHORT_SCHEMA_TOKEN = re.compile(r"`/(\d+)`")


def _the_line(relative: str, prefix: str) -> str:
    """The one line of `relative` that starts with `prefix`, or a failure.

    The pages state each schema list in exactly one sentence or table row, so
    an ambiguous or missing locator is itself a documentation change that has
    to be looked at.
    """
    text = (ROOT / relative).read_text(encoding="utf-8")
    (line,) = [line for line in text.splitlines() if line.startswith(prefix)]
    return line


def test_documented_schema_lists_match_the_accepted_schemas() -> None:
    """Pin every file-loading boundary and the separate workflow-less builder."""
    from evolution.tree import conformance_manifest, evolve
    from evolution.tree.manifest import manifest_v2

    accepted = {
        str(getattr(manifest_v2, name)).rsplit("/", 1)[-1]
        for name in dir(manifest_v2)
        if re.fullmatch(r"SCHEMA_V\d+", name)
    }
    campaign = {schema.rsplit("/", 1)[-1] for schema in evolve.CAMPAIGN_SCHEMAS}
    assert accepted == campaign == {"15", "16"}
    assert len(evolve.CAMPAIGN_SCHEMAS) == 2
    refused = {str(version) for version in range(1, 15)}

    def exact_list(text: str, expected: set[str]) -> None:
        tokens = SCHEMA_TOKEN.findall(text)
        assert set(tokens) == expected and len(tokens) == len(expected), text

    # Loader and command refusals enumerate every removed schema, not a subset.
    row = _the_line("docs/manifest-v2.md", "| `manifest_schema` |")
    removed, marker, produce = row.partition("only")
    assert marker, row
    exact_list(removed, refused)
    exact_list(produce, accepted)
    assert "Every other campaign schema" in row
    assert "expected evolution-v2-manifest/15 or /16" in row

    line = _the_line("docs/manifest-v2.md", "`schema` is the literal string")
    assert set(FULL_SCHEMA_TOKEN.findall(line)) == accepted, line
    assert len(FULL_SCHEMA_TOKEN.findall(line)) == 2
    assert set(SHORT_SCHEMA_TOKEN.findall(line)) == refused, line
    assert len(SHORT_SCHEMA_TOKEN.findall(line)) == 14
    assert "manifest_schema at schema" in line and "Every other value" in line

    # Every file-loading command needs a complete native manifest, even conformance.
    for relative, prefix in (
        ("docs/manifest-v2.md", "File loading accepts only"),
        ("docs/manifest-v2.md", "`evo tree conformance --manifest PATH` takes a full"),
        ("docs/cli.md", "`evo tree conformance` takes a full"),
        ("docs/cli.md", "`--manifest` is the "),
        ("docs/cli.md", "| `--manifest PATH` |"),
        ("docs/cli.md", "`evo-target serve` loads only"),
    ):
        line = _the_line(relative, prefix)
        exact_list(line, accepted)

    line = _the_line("docs/manifest-v2.md", "For Python integrations that need no")
    assert "exactly `seed_root`, `target` and `artifacts`" in line
    assert "`schema` and every other extra key are refused as `unknown_key`" in line
    assert "library API, not a CLI flag or a new file schema" in line
    assert conformance_manifest is manifest_v2.conformance_manifest
    metadata = _the_line("docs/manifest-v2.md", "The builder uses the shared target")
    for boundary in (
        "workflow=None",
        "in-memory `/1` schema label",
        'base.resolve() / "manifest.json"',
        "never reads, creates or checks",
        "do not admit `/1`",
        "workflow_not_parsed",
    ):
        assert boundary in metadata


def test_reflector_failure_docs_distinguish_content_timeouts_and_skips() -> None:
    from evolution.tree.roles.role_failures import reflection_class

    text = (ROOT / "docs/manifest-v2.md").read_text(encoding="utf-8")
    unplanned = text.split("### Unplanned rounds (family 9)\n", 1)[1].split(
        "### Reflector and Editor resilience (family 10)\n", 1
    )[0]
    (failed,) = [
        line for line in unplanned.splitlines() if line.startswith("| `reflection_failed` |")
    ]
    assert "`content`" in failed and "timeout" not in failed

    rows = re.findall(r"^\| `(content|provider|unknown)` \| (.+) \|$", unplanned, flags=re.M)
    assert len(rows) == 3
    actions = dict(rows)
    assert set(actions) == {"content", "provider", "unknown"}
    for error, reason, action in (
        ("invalid_result", "invalid_result", "unplanned/reflection_failed"),
        ("reported", "provider_exhausted", "reflector_provider_failed"),
        ("timeout", "timeout", "reflector_failing"),
    ):
        assert f"`{action}`" in actions[reflection_class(error, reason)]
    for failure_class in ("provider", "unknown"):
        assert "suspend" in actions[failure_class].lower()
        assert "round stays open" in actions[failure_class]
        assert "unplanned" not in actions[failure_class]
    assert "`timeout`" in actions["unknown"]
    for contract in (
        "reflector.max_consecutive_failures",
        'visitation: "skipped"',
        "same error group",
        "two consecutive skips",
        "reflector_failing",
    ):
        assert contract in actions["content"]


FAMILY_TOKEN = re.compile(r"family[- ]`(\d+)`|`(\d+)`")


def test_the_v15_parameter_table_is_generated_from_parameter_help() -> None:
    from evolution.tree.manifest.campaign_policy_help import render_v15_parameter_table

    text = (ROOT / "docs/manifest-v2.md").read_text(encoding="utf-8")
    begin, end = "<!-- BEGIN v15-parameters -->", "<!-- END v15-parameters -->"
    assert text.count(begin) == 1 and text.count(end) == 1
    between = text.partition(begin)[2].partition(end)[0]
    assert between.startswith("\n\n") and between.endswith("\n\n"), "blank lines around the table"
    assert between.strip() == render_v15_parameter_table().strip()


def test_manifest_v2_documents_the_15_sections() -> None:
    """The `/15` reference sections `evolve --help` and the operator guides link to."""

    lines = (ROOT / "docs/manifest-v2.md").read_text(encoding="utf-8").splitlines()
    for heading in (
        "### Stable family keys (manifest /15)",
        "#### What family 14 measures",
        "### Move a /14 manifest to /15",
        "## `grouper`",
        "### `scorer.metric_contract`",
        "## `role_visibility`",
    ):
        assert lines.count(heading) == 1, heading
    assert any(line.startswith("| `projection_changed` |") for line in lines)


def test_cli_page_command_tree_matches_the_registered_tree_and_top_level() -> None:
    from typer.main import get_command

    from evolution.cli.main import app

    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    tree_row = re.search(r"^\| `evo tree` \| (.+) \|$", text, flags=re.M)
    assert tree_row is not None
    documented_tree = set(re.findall(r"`([a-z-]+)`", tree_row.group(1)))
    assert documented_tree == set(get_command(app).commands["tree"].commands)

    top_row = re.search(r"^\| top level \| (.+) \|$", text, flags=re.M)
    assert top_row is not None
    documented_top = set(re.findall(r"`([a-z-]+)`", top_row.group(1)))
    top_level = {
        name
        for name, command in get_command(app).commands.items()
        if not hasattr(command, "commands")
    }
    assert documented_top == top_level


def test_maintained_docs_name_neither_the_gui_command_nor_its_extra() -> None:
    for relative in ("README.md", "QUICKSTART.md", "docs/cli.md", "docs/SUMMARY.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "evo gui" not in text, relative
        assert "[gui]" not in text, relative
        assert "Evolution 0.2" not in text, relative
    for relative in ("docs/architecture.md", "docs/configuration.md"):
        assert "Evolution 0.2" not in (ROOT / relative).read_text(encoding="utf-8"), relative


def test_summary_names_the_preview_version() -> None:
    import evolution

    summary = (ROOT / "docs" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "(in progress)" not in summary
    assert evolution.__version__ in summary


def test_the_b3_role_feedback_surface_is_documented() -> None:
    manifest = (ROOT / "docs" / "manifest-v2.md").read_text(encoding="utf-8")
    for needle in (
        "## `verbatim_answers`",
        "`verbatim_answers/1`",
        "`verbatim_answer_unmatched`",
        "`changed_file_budget`",
        "`written_byte_budget`",
    ):
        assert needle in manifest, needle
    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert "`verbatim_answer_unmatched`" in cli and "verbatim_answers.strings[" in cli
    guide = (ROOT / "docs" / "running-campaigns.md").read_text(encoding="utf-8")
    assert "`changed_file_budget`" in guide and "`verbatim_answers`" in guide
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("## [2.0.0b3]", 1)[1].split("\n## [", 1)[0]
    assert "`changed_file_budget`" in section


def test_the_b12_role_check_surface_is_documented() -> None:
    manifest = (ROOT / "docs" / "manifest-v2.md").read_text(encoding="utf-8")
    for needle in (
        "## `secret_oracle_paths`",
        "`secret_oracle_paths/1`",
        "`secret_oracle_path_unmatched`",
        "`pointers: []`",
        "`plan_correction/1`",
        "`plan_path_not_writable`",
        "`detail`",
        "`leak`",
    ):
        assert needle in manifest, needle
    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert "`secret_oracle_path_unmatched`" in cli and "secret_oracle_paths.pointers[" in cli
    guide = (ROOT / "docs" / "running-campaigns.md").read_text(encoding="utf-8")
    assert "`secret_oracle_paths`" in guide and "last protocol error" in guide
    section = (
        (ROOT / "CHANGELOG.md")
        .read_text(encoding="utf-8")
        .split("## [2.0.0b12]", 1)[1]
        .split("\n## [", 1)[0]
    )
    assert (
        "`secret_oracle_paths`" in section
        and "`plan_correction/1`" in section
        and "rebase" in section
    )


def test_localized_entry_guides_preserve_commands_and_language_routes() -> None:
    for localized in LOCALIZED_DOCS:
        english = localized.replace(".ru.md", ".md")
        original = (ROOT / english).read_text(encoding="utf-8")
        translated = (ROOT / localized).read_text(encoding="utf-8")
        assert localized in CORE_DOCS
        assert _markdown_classifications()[localized] == "maintained_documentation"
        assert CYRILLIC.search(translated)
        assert f"]({localized})" in original
        assert f"]({english})" in translated
        fences = r"^```[^\n]*\n(.*?)^```"
        assert re.findall(fences, original, re.M | re.S) == re.findall(
            fences, translated, re.M | re.S
        ), f"{localized}: translated command/example blocks differ"


def test_nested_distributions_own_their_markdown() -> None:
    tracked = {
        "pyproject.toml",
        "README.md",
        "docs/usage.md",
        "extension/pyproject.toml",
        "extension/README.md",
        "extension/docs/guide.md",
        "docs/examples/package/pyproject.toml",
        "docs/examples/package/README.md",
    }
    assert _owned_markdown(tracked) == {
        "README.md",
        "docs/usage.md",
        "docs/examples/package/README.md",
    }
