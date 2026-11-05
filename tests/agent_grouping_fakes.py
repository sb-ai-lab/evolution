"""Offline, genuinely bound train input for the agent grouping helper tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from evolution.tree.execution.corpus_partition import load_partition
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.execution.journal_identity import case_payload
from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
from tests.campaign_fakes import PRIMARY
from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace

TEST_SENTINEL = "TEST-ORACLE"
TEST_PUBLIC_SENTINEL = "TEST-PUBLIC-task7"
TEST_PRIVATE_SENTINEL = "TEST-PRIVATE-task7"


@contextmanager
def grouping_inputs(
    root: Path, *, samples: int = 1, turns: int = 1
) -> Iterator[FrozenReflectionInputs]:
    """A real trace reader over an explicit train/test corpus partition."""
    trace = _Trace(
        root, turns=turns, samples=samples, diagnostics={"train_diagnostic": "available"}
    )
    try:
        held_out = EvalCase(
            "test",
            "test-group",
            "s",
            (),
            {"text": TEST_PUBLIC_SENTINEL},
            {"secret": TEST_PRIVATE_SENTINEL},
            {"secret": TEST_SENTINEL},
        )
        partition_path = root / "partition.jsonl"
        partition_path.write_text(
            "\n".join(
                json.dumps(case_payload(case) | {"split": split})
                for case, split in ((trace.case, "train"), (held_out, "test"))
            ),
            encoding="utf-8",
        )
        partition = load_partition(partition_path)
        assert partition.train.cases == (trace.case,)
        assert partition.test.cases == (held_out,)
        yield FrozenReflectionInputs(
            baseline_binding=trace.baseline.digest,
            rows=tuple(_reflect_row(f"s{index}", index) for index in range(1, samples + 1)),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=partition.train.cases,
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
            reflection_metadata={"c": {"topic": "routing"}},
            families={"c": "family-a"},
            scorer_diagnostics=True,
        )
    finally:
        trace.close()


class ScriptedGroupingAgent:
    """A fresh backend per script step, with real file writes and telemetry."""

    def __init__(self, script, *, turns=None):
        self.script = list(script)
        self.turns = list(turns or [1] * len(self.script))
        self.calls = []
        self.factories = []
        self.content = "done"

    def factory(self, **kwargs):
        from types import SimpleNamespace

        self.factories.append(kwargs)
        owner = self

        class Backend:
            last_usage = {}
            last_codex_diagnostics = {}

            async def complete_file_edit(self, root, opening, system, **_kwargs):
                import inspect

                owner.calls.append((root, opening, system))
                self.last_usage = {"tool_turns": owner.turns.pop(0)}
                item = owner.script.pop(0)
                if callable(item):
                    item = item(root)
                    if inspect.isawaitable(item):
                        item = await item
                if isinstance(item, BaseException):
                    raise item
                if item is not None:
                    (root / ".evolution/GROUPS.json").write_text(item, encoding="utf-8")
                return SimpleNamespace(content=owner.content)

        return Backend()


GROUP_ALL_AGENT_BODY = """
entries = []
offset = 0
while offset is not None:
    page = helper("list_grouping_inputs", {"offset": offset})
    entries.extend(page["entries"])
    offset = page["next_offset"]
failing = [entry for entry in entries if entry["failing"] and entry["available"]]
for entry in failing:
    identity = helper("read_grouping_input", {"index": entry["index"], "section": "identity"})
    entry["case_id"] = json.loads(identity["content"])["case_id"]
(work / ".evolution/GROUPS.json").write_text(json.dumps({"groups": [{
    "id": "all", "title": "shared failures", "failure_mode": "observed failures",
    "expected_behaviour": "criteria pass", "question": "which observations failed?",
    "members": [{"evidence_id": entry["evidence_id"], "variant": "failure"} for entry in failing],
    "affected_cases": sorted({entry["case_id"] for entry in failing}),
    "citations": [], "priority": "normal"
}]}))
"""


def sealed_grouping_cli(root: Path, *, body: str | None = None, **overrides):
    """A real offline Codex-protocol executable, sealed by evolve like any CLI.

    Each invocation records its projection and supplied token hash outside the
    disposable directory. The default body reads genuine helper observations
    and groups all available failures; custom bodies share `work`, `request`
    and `helper` and can exercise real process failures and repair files.
    """
    import sys

    program = root / "grouper-cli"
    trace = root / "grouper-invocations.jsonl"
    program.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "sys.stdin.read()\n"
        "work = Path(sys.argv[sys.argv.index('-C') + 1])\n"
        "request = json.loads((work / '.evolution/request.json').read_text())\n"
        "def helper(name, args):\n"
        "    return json.loads(subprocess.check_output([*request['helpers'][name], json.dumps(args)]))\n"
        f"with Path({str(trace)!r}).open('a') as trace:\n"
        "    trace.write(json.dumps({'root': str(work), 'request': request, 'pid': os.getpid(),\n"
        "        'repair': (work / '.evolution/VALIDATION.json').exists(),\n"
        "        'token_sha256': hashlib.sha256(os.environ.get('AGENT_TOKEN', '').encode()).hexdigest()}) + '\\n')\n"
        + (GROUP_ALL_AGENT_BODY if body is None else body)
        + "\nPath(sys.argv[sys.argv.index('-o') + 1]).write_text('done')\n"
        + "print(json.dumps({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'group train observations', 'exit_code': 0}}))\n",
        encoding="utf-8",
    )
    program.chmod(0o755)
    return {
        "kind": "agent_cli",
        "model": "codex",
        "agent_bin": str(program),
        "timeout_seconds": 60,
        **overrides,
    }


def scripted_grouping_cli(root: Path, *groupings: str, **overrides):
    """Seal a real CLI whose successive operations follow a fixed script.

    Repair calls share an operation root. Counting distinct recorded roots
    therefore advances on regrouping (including resume), never on repair.
    The script is embedded in the sealed program, not edited between runs.
    """
    trace = root / "grouper-invocations.jsonl"
    body = (
        f"calls = [json.loads(line) for line in Path({str(trace)!r}).read_text().splitlines()]\n"
        "operations = list(dict.fromkeys(call['root'] for call in calls))\n"
        f"mode = {groupings!r}[len(operations) - 1]\n"
        "repair = (work / '.evolution/VALIDATION.json').exists()\n"
        "if mode == 'agent_error':\n    sys.exit(7)\n"
        "if mode == 'hang' or (mode == 'repair_hang' and repair):\n"
        "    import time\n    time.sleep(30)\n"
        "if mode == 'turns_exhausted':\n"
        "    for index in range(5):\n"
        "        print(json.dumps({'type': 'item.started', 'item': "
        "{'type': 'command_execution', 'command': str(index), 'exit_code': 0}}))\n"
        "if mode in ('admitted', 'partial'):\n"
        + "\n".join("    " + line for line in GROUP_ALL_AGENT_BODY.splitlines())
        + "\n    if mode == 'partial':\n"
        "        output = work / '.evolution/GROUPS.json'\n"
        "        groups = json.loads(output.read_text())\n"
        "        groups['groups'][0]['members'] = groups['groups'][0]['members'][:1]\n"
        "        output.write_text(json.dumps(groups))\n"
        "elif mode in ('invalid_output', 'repair_hang'):\n"
        "    (work / '.evolution/GROUPS.json').write_text('{malformed')\n"
        "else:\n"
        "    assert mode in ('missing', 'turns_exhausted', 'hang'), mode\n"
    )
    return sealed_grouping_cli(root, body=body, **{"max_turns": 4, **overrides})
