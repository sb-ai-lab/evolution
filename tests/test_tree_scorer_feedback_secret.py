"""A scorer's run secret must not reach a role's input through its own output.

A scorer's `allowed_env` values are run secrets (manifest reference, "What the
scorer program reads and writes"). The scorer's `public_feedback`, verdicts and
`diagnostics` are shown to the Editor, the Reflector and the Grouper; a scorer
that echoes its credential there would hand it to those roles and, for a
`chat` or `agent_cli` role, to its model provider (#313).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution.tree.evolve import evolve
from tests import fake_chat_server
from tests.test_tree_evolve import (
    CHAT_KEY,
    CHAT_REFLECT_SCRIPT,
    chat_reflector_section,
    fake_adapters,
)
from tests.tree_evolve_support import CAMPAIGN_PAYLOAD, write_workspace

pytestmark = pytest.mark.slow

TOKEN = "scorer-token-7f3a9c2e51"

RECORDING_EDITOR = r"""
import json
import pathlib
import sys

request = json.loads(sys.stdin.read())
pathlib.Path(f"brief-{request['attempt_id']}.json").write_text(json.dumps(request["brief"]), encoding="utf-8")
skill = pathlib.Path(request["workspace"]) / "skills" / "SKILL.md"
skill.write_text(skill.read_text(encoding="utf-8").rstrip("\n") + " edited\n", encoding="utf-8")
print(json.dumps({"status": "edited", "note": "recorded"}))
"""

ECHOING_SCORER = r"""
import json
import os
import sys

json.load(sys.stdin)
json.dump(
    {
        "metrics": {"score": 0.0},
        "critical_pass": True,
        "verdicts": [{"criterion": "answered", "passed": False}],
        "public_feedback": {"judge_key": os.environ["SCORER_TOKEN"]},
    },
    sys.stdout,
)
"""


async def test_scorer_secret_in_public_feedback_never_reaches_the_editor_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCORER_TOKEN", TOKEN)
    ws = tmp_path / "ws"
    manifest = write_workspace(
        ws,
        scorer_script=ECHOING_SCORER,
        editor_script=RECORDING_EDITOR,
        scorer_env=("SCORER_TOKEN",),
        campaign={**CAMPAIGN_PAYLOAD, "round_stop": "all_attempts"},
    )
    await evolve(manifest, ws / "run", adapter_factory=fake_adapters())

    briefs = sorted((ws / "editor").glob("brief-*.json"))
    assert briefs, "the Editor never ran, so the brief path was not exercised"
    for path in briefs:
        assert TOKEN not in path.read_text(encoding="utf-8"), path.name
    # The entry is dropped and counted, as an entry outside the grammar is.
    observations = [
        repeat["observation"]
        for case in json.loads(briefs[0].read_text(encoding="utf-8"))["cases"]
        for repeat in case["repeats"]
    ]
    assert observations
    for observation in observations:
        assert observation["feedback"] == {}
        assert observation["feedback_omitted"] == 1


PROSE_SCORER = r"""
import json
import os
import sys

json.load(sys.stdin)
token = os.environ["SCORER_TOKEN"]
json.dump(
    {
        "metrics": {"score": 0.0},
        "critical_pass": True,
        "verdicts": [{"criterion": "answered", "passed": False, "note": "judge key " + token}],
        "public_feedback": {"judge": "auth failed for " + token},
        "diagnostics": {"judge_key": token},
    },
    sys.stdout,
)
"""


async def test_scorer_secret_never_reaches_a_chat_reflector_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCORER_TOKEN", TOKEN)
    monkeypatch.setenv("E1_KEY", CHAT_KEY)
    base_url, server = fake_chat_server.start(CHAT_REFLECT_SCRIPT)
    try:
        ws = tmp_path / "ws"
        manifest = write_workspace(
            ws,
            scorer_script=PROSE_SCORER,
            scorer_env=("SCORER_TOKEN",),
            reflector=chat_reflector_section(base_url),
        )
        await evolve(manifest, ws / "run", adapter_factory=fake_adapters())
    finally:
        server.shutdown()
        server.server_close()

    # The scripted Reflector read the evidence, so the answer to that read
    # was sent back to the provider in a later request.
    assert len(server.script.requests) >= 4
    assert TOKEN not in json.dumps(server.script.requests)
