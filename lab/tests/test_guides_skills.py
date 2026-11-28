"""W21 §7.2: skills carry frontmatter and self-contained relative links."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
import tomllib

from evolution_lab import guides

GUIDES = Path(guides.__file__).parent
GUIDE = Path(__file__).resolve().parents[2] / "docs" / "guide"
LINK = re.compile(r"\]\(([^)\s]+)\)")
SKILLS = {"requirements", "target-connector", "evaluation-inputs", "campaign-package",
          "diagnostics", "pitfalls", "preparation", "budget", "first-evaluation", "campaign-operations"}


def test_failure_variant_table_matches_the_agreed_contract():
    text = (GUIDES / "campaign-package" / "SKILL.md").read_text(encoding="utf-8")
    section = text.split('<a id="run-failures"></a>', 1)[1].split('<a id="campaign-scale">', 1)[0]
    rows = [line.split("|")[2:5] for line in section.splitlines() if line.startswith("| ")]
    assert [[cell.strip() for cell in row] for row in rows] == [
        ['`retry` «С повтором» (умолчание)', '`no_retry` «Без повтора»', '`fail_fast` «Остановка на первом сбое»'],
        ["3", "0", "0"], ["20", "20", "0"], ['`"1"`', '`"1"`', '`"0"`'],
        ["5", "5", "1"], ["3", "3", "1"], ["2", "1", "1"],
        ["= `max_turns`", "= `max_turns`", "1"], ["3", "0", "0"],
        ["0.95", "0.95", "1"], ["3", "3", "1"], ["3 / 2", "3 / 1", "1 / 1"],
        ["= `max_turns`", "= `max_turns`", "1"],
    ]


def test_guides_and_catalogue_avoid_rejected_failure_terminology():
    paths = [*GUIDES.rglob("*.md"), *GUIDES.rglob("*.json"),
             *GUIDE.rglob("*.md")]
    for path in paths:
        assert not re.search("терпим", path.read_text(encoding="utf-8"), re.IGNORECASE), path


def test_current_service_evaluation_example_uses_the_closed_v5_form():
    from evolution_lab.service.codecs import PREPARE_REQUEST_SCHEMA, decode_prepare_request

    text = Path("docs/api/service-s5.md").read_text(encoding="utf-8")
    section = text.split("The corresponding evaluation prepare file", 1)[-1]
    # Select the complete evaluation request, not a Bundle or a launch document.
    example = next(json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", section, re.S)
                   if '"kind": "evaluation", "bundle_digest": "BUNDLE_DIGEST"' in raw)
    example["bundle_digest"] = "a" * 64
    decoded = decode_prepare_request({"schema": PREPARE_REQUEST_SCHEMA, **example})
    assert PREPARE_REQUEST_SCHEMA == "lab-service-prepare-request/5"
    assert decoded.kwargs["max_consecutive_unmeasured"] is None


def test_every_skill_has_unique_frontmatter_name_and_description():
    found = guides.list_guides()
    assert {info.path.split("/")[0] for info in found} == SKILLS
    assert all(info.name and info.description for info in found)
    assert len({info.name for info in found}) == len(found)
    assert {info.name for info in found} >= {
        "evolution-lab-requirements", "evolution-lab-target-connector",
        "evolution-lab-evaluation-inputs", "evolution-lab-campaign-package",
        "evolution-lab-diagnostics", "evolution-lab-pitfalls"}


def test_relative_links_stay_inside_guides():
    base = GUIDES.resolve()
    for path in GUIDES.rglob("*.md"):
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            assert resolved.is_relative_to(base), (path.name, target)
            assert resolved.exists(), (path.name, target)


def test_feeds_grammar_says_feeds_are_alternatives():
    from evolution_lab import guides
    text = (Path(guides.__file__).parent / "requirements" / "SKILL.md").read_text(encoding="utf-8")
    section = text.split('<a id="feeds"></a>', 1)[1].split("<a id=", 1)[0]
    assert "`obligation`" in section
    assert "may end up" in section


def test_skills_admit_manifest_15_and_16():
    """Current admission is /15 and /16 on Owner paths; dev50 documentation describes family14-only reading."""
    for path in GUIDES.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "as_manifest_14" not in text, path.name
        assert "as_manifest_11" not in text, path.name
        assert "0.3.0a28" not in text, path.name
    package = (GUIDES / "campaign-package" / "SKILL.md").read_text(encoding="utf-8")
    for needle in ("Lab accepts `evolution-v2-manifest/15` and `evolution-v2-manifest/16`", "unsupported_schema",
                   "gate.mode", "confidence", "execution_noise", "linked_run_unsupported",
                   "promotion is automatic", "as_manifest_15"):
        assert needle in package, needle
    for needle in ("`evidence`", "`checks`", "Role-loop limits",
                   "`campaign.indeterminate.<owner>.*`", "`target.preflight_sample_retries`",
                   "`reflector.probe.string_body`", "qwen/qwen3.8-flash", 'string_body: "parse"',
                   "even when independent acceptance data is", "Counts from different checks are not comparable",
                   "Finish `/16` campaigns started on dev60/b12"):
        assert needle in package, needle
    core_docs = "https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md"
    for anchor in ("evidence-manifest-16", "checks-manifest-16", "role-loop-limits-manifest-16",
                   "campaign", "schema-seed_root-target-and-artifacts", "optional-live-probes"):
        assert f"{core_docs}#{anchor}" in package, anchor
    for needle in ("observe", "statistical", "repeats_per_question", "split"):  # the /15 section keeps its words
        assert needle in package, needle
    preparation = (GUIDES / "preparation" / "SKILL.md").read_text(encoding="utf-8")
    assert "lab-service-prepare-request/3" in (GUIDES / "evaluation-inputs" / "SKILL.md").read_text(encoding="utf-8")
    assert "`/15`" in preparation


def test_connector_reference_follows_entry_validator_and_setup_order():
    from evolution_lab.projects.entry import DEFAULT_LIMITS, _OPTIONAL, _REQUIRED

    text = (GUIDES / "target-connector" / "SKILL.md").read_text(encoding="utf-8")
    headings = ["## Setup order:", "### Field reference: `lab-target-entry/1`",
                "## Neutral turn contract", "## Import-and-build self-check recipe"]
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)
    reference = text.split(headings[1], 1)[1].split("## The four seams", 1)[0]
    assert "`evolution_lab.projects.entry`" in reference
    fields = set(re.findall(r"^\| `([^`]+)` \|", reference, re.MULTILINE))
    assert fields == _REQUIRED | _OPTIONAL | {"agent.import_root"}
    assert '**Not `null`** in entry `/1`' in reference
    assert 'launch `/2` allows `agent.import_root: null`' in reference
    assert '"kind": "service"' in reference and "**outside** the place" in reference
    assert "positive integer, not a boolean" in reference
    entry = json.loads(re.search(r"```json\n(.*?)\n```", text, re.DOTALL).group(1))
    assert set(entry) == _REQUIRED | _OPTIONAL
    assert entry["limits"] == DEFAULT_LIMITS
    assert (entry["startup_timeout_seconds"], entry["stop_grace_seconds"]) == (30, 5)
    contract = text.split("## Neutral turn contract", 1)[1].split("```text\n", 1)[1].split("```", 1)[0]
    assert all(name in contract for name in ("TurnAgent", "TurnReason", "LaunchContext", "AgentFactory"))


def test_connector_self_check_is_isolated_build_only_recipe_text():
    text = (GUIDES / "target-connector" / "SKILL.md").read_text(encoding="utf-8")
    recipe = text.split('## Import-and-build self-check recipe', 1)[1].split("## Current path:", 1)[0]
    code = re.search(r"```python\n(.*?)\n```", recipe, re.DOTALL).group(1)
    # Parse the published recipe, never execute connector code or the recipe.
    tree = ast.parse(code)
    run = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and node.func.attr == "run")
    argv = run.args[0]
    assert isinstance(argv, ast.List)
    assert [ast.literal_eval(arg) for arg in argv.elts[1:3]] == ["-I", "-c"]
    keywords = {keyword.arg: keyword.value for keyword in run.keywords}
    assert ast.unparse(keywords["env"]) == "{**entry['env'], **supplied}"
    assert ast.unparse(keywords["cwd"]) == "work_directory"
    child = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "code" for target in node.targets))
    calls = [ast.unparse(node.func) for node in ast.walk(ast.parse(child)) if isinstance(node, ast.Call)]
    assert "build" in calls and "callable" in calls
    assert "factory" not in calls and "isinstance" not in calls
    for needle in ('entry["environment"]["executable"]', 'json.loads(secrets_path.read_text',
                   'from evolution_lab.target import LaunchContext', 'os.environ.items()',
                   'work_directory=Path.cwd()', 'sys.path.insert(0, agent["import_root"])',
                   'importlib.import_module(agent["module"])',
                   'factory = build(context)', "type alias", "candidate snapshot",
                   "not guaranteed to be free", "not a prepared Bundle"):
        assert needle in recipe, needle


def test_connector_describe_outcome_and_probe_cleanup_rules():
    text = (GUIDES / "target-connector" / "SKILL.md").read_text(encoding="utf-8")
    for needle in ('TurnReason("agent_outcome", "answered")', '`unspecified`',
                   "no protocol self-test", "bundle_probe_only", "settings_sha256",
                   "scorer `allowed_env`", "`agent_cli` environment", "model names, URLs",
                   "8 characters or more", "judge's model name", "regardless of which literal values",
                   "must be greater than the agent's", "whole probe target",
                   "unconfirmed process-group stop", "surviving mutating I/O",
                   "cleanup_required", "`lab agent stop`"):
        assert needle in text, needle


def test_current_pair_guides_name_the_pair_and_delivery_pointer():
    section = Path("docs/RELEASES.md").read_text(encoding="utf-8").split("## Released pairs\n", 1)[1]
    rows = [line for line in section.splitlines() if line.startswith("|")][2:]
    pairs = [[cell.strip().strip("`") for cell in row.strip("|").split("|")][:2] for row in rows]
    # A wheel's guides name the release before it; its own row lands only in the release commit, which leaves the
    # wheel bytes unchanged, so the check holds both before and after that commit.
    source = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    version, core = next(pair for pair in pairs if pair[0] != source)
    current_advice = {
        relative: f"latest released pair is Lab `{version}` / exact core `{core}`"
        for relative in ("service/README.md", "owner/README.md", "preparation/SKILL.md",
                         "campaign-package/SKILL.md", "diagnostics/SKILL.md")
    }
    for relative, advice in current_advice.items():
        text = (GUIDES / relative).read_text(encoding="utf-8")
        assert advice in text, relative
        assert "docs/RELEASES.md" in text, relative
        assert "fresh" in text.lower(), relative
        assert "development source" in text, relative


def test_workspace_skills_follow_the_autonomy_level():
    names = [
        "scenarios/connect", "scenarios/create-skill", "scenarios/evolve",
        "scenarios/restructure", "campaign-operations",
    ]
    for name in names:
        text = (GUIDES / name / "SKILL.md").read_text(encoding="utf-8")
        assert all(term in text for term in ("`ask`", "`yolo`", "MCP", "conversation")), name
        assert "never by an assistant" not in text and "Only the person" not in text, name
