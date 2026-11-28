"""Capture the installed b13 Editor loop on its Core-built common /6 brief.

Campaign execution attaches feedback and sends /7. This separate direct role
invocation exercises the preceding /6 form without claiming a campaign used it.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import sys
import uuid

from fake_chat import FakeChat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--retained", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo / "tests"))
    from scaffold_16 import scaffold_16
    from evolution.prompts import TREE_EDITOR_SYSTEM
    from evolution.tree import load_manifest_v2
    from evolution.tree.campaign_editor import Diagnosis, DiagnosisFocus, EditRequest, brief_payload, build_brief
    from evolution.tree.chat_editor import ChatCredentials, ChatEditor
    from evolution.tree.editor_seal import seal_prompt
    from evolution.tree.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
    from evolution.tree.manifest_v2 import ChatEditorSection

    target = Path(__file__).resolve().parent / "b13" / "editor6"
    if target.exists():
        raise RuntimeError(f"preserve prior capture: {target}")
    run = args.retained / ("editor6-" + uuid.uuid4().hex)
    run.mkdir(parents=True, mode=0o700)
    shutil.copyfile(__file__, run / "capture_b13_editor6.py")
    sample = scaffold_16(run / "neutral-input")
    manifest = load_manifest_v2(sample.manifest)
    brief = build_brief([], [], manifest.workflow.campaign, "0" * 64,
        diagnosis=Diagnosis("absent", None, DiagnosisFocus("selected", ()), (), None),
        pin=EvidencePin(manifest.workflow.evidence or DEFAULT_EVIDENCE, {}))
    assert brief.version == "edit_brief/6"
    payload = brief_payload(brief)
    (run / "brief.json").write_text(json.dumps(payload, indent=1) + "\n")
    fake = FakeChat()
    try:
        editor = ChatEditor(section=ChatEditorSection("fake-model", f"http://127.0.0.1:{fake.port}/v1", None,
                max_turns=16, context_message_max_chars=12000),
            credentials=ChatCredentials(None, None), prompt=seal_prompt(None, packaged=TREE_EDITOR_SYSTEM),
            log_root=run / "editor-logs", secrets=(), family15=True, plan_correction=True)
        result = asyncio.run(editor.edit(EditRequest("r01-a01", 1, sample.seed_root,
            ("skills/**",), brief, 60)))
        assert result.status == "edited", result
    finally:
        (run / "fake-chat.json").write_text(json.dumps(fake.requests, indent=1) + "\n")
        fake.close()
    meta = {"schema": "lab-direct-role-fixture/1", "core_version": version("evo-skills"),
        "lab_version": version("evolution-lab"), "captured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "capture_id": run.name, "invocation": "direct_role_not_campaign", "brief_version": brief.version,
        "core_functions": ["evolution.tree.campaign_editor.build_brief", "evolution.tree.chat_editor.ChatEditor.edit"],
        "producing_wheels": {"core": {"file": args.core_wheel.name,
            "sha256": hashlib.sha256(args.core_wheel.read_bytes()).hexdigest()}},
        "generator": {"path": str(Path(__file__).relative_to(repo)),
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "result": {"status": result.status, "code": result.code}}
    target.mkdir(parents=True)
    (target / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    shutil.copyfile(run / "brief.json", target / "brief.json")
    logs = target / "run" / "direct-editor6" / "editor-logs"
    logs.mkdir(parents=True)
    shutil.copyfile(run / "editor-logs" / "r01-a01.jsonl", logs / "r01-a01.jsonl")
    print(f"direct /6 Editor capture: {run}")


if __name__ == "__main__":
    main()
