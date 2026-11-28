"""Demo Editor wrapper for the panel fixture capture: hold while <wc>.hold exists, fail while
<wc>.editor-fault exists, otherwise act by the per-call script and default to the original Editor.

``<wc>.editor-script`` is a JSON list of modes; each call pops the first one (the file is rewritten
without it; an empty list or no file means ``pass``):

- ``pass``   exec the original Editor;
- ``plain``  an edit that does not improve: append a line to skills/SKILL.md (its first line, which
             the demo scores, stays) and add skills/NOTES.md;
- ``refuse`` the Editor answers ``{"status": "declined", ...}`` and exits 0. What core a21 does with
             a subprocess Editor's answer (evolution/tree/subprocess_editor.py ``_classify``, then
             campaign_view.py ``next_step`` and role_failures.py): a non-zero exit, unparsable stdout or
             ``{"status": "failed"}`` all become ``failed`` with an ``unknown`` class (``program_failed``),
             which a resilient campaign does not close but suspends (``editor_failing``, then the
             attempt is re-run on resume). The ``content`` classes an Editor failure needs to close the
             attempt as ``abandoned/editor_failed`` (``protocol_exhausted``, ``turns_exhausted``,
             ``self_check_failed``, ``actor_violation``, ``plan_mismatch``) are produced only by the
             agent Editor (chat_editor / agent_editor), never by the subprocess protocol. The one
             answer that ends an attempt without a suspension is ``declined``: core closes it
             ``abandoned/editor_declined``.

The values come from ``editor_hold.json`` next to this file, ``{"flag_base", "original_argv"}``:
the manifest gives a subprocess Editor no environment (``allowed_env: []``) and the Bundle refuses
an argv operand that looks like a path outside its roots, so, like ``_wrap_editor`` in
tests/test_service_installed_role_suspension.py, the values travel in the materials themselves.
capture.py copies this file and writes the JSON into the demo's ``lab/editor`` directory; the
wrapper never runs from ``tests/``.
"""
import json
import os
import sys
import time
from pathlib import Path

config = json.loads(Path(__file__).with_name("editor_hold.json").read_text(encoding="utf-8"))
flag_base = Path(config["flag_base"])
original = config["original_argv"]
hold = flag_base.with_name(flag_base.name + ".hold")
fault = flag_base.with_name(flag_base.name + ".editor-fault")
deadline = time.monotonic() + 600
while hold.exists() and time.monotonic() < deadline:
    time.sleep(0.2)
if fault.exists():
    print("fixture: editor fault flag present", file=sys.stderr)
    sys.exit(1)


def _next_mode() -> str:
    script = flag_base.with_name(flag_base.name + ".editor-script")
    try:
        modes = json.loads(script.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "pass"
    if not modes:
        return "pass"
    script.write_text(json.dumps(modes[1:]), encoding="utf-8")
    return modes[0]


def _plain() -> None:
    request = json.load(sys.stdin)
    workspace = Path(request["workspace"])
    skill = workspace / "skills" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "Plain fixture note.\n", encoding="utf-8")
    (workspace / "skills" / "NOTES.md").write_text("fixture notes\n", encoding="utf-8")
    print(json.dumps({"status": "edited", "note": "fixture: no improvement"}))


mode = _next_mode()
if mode == "plain":
    _plain()
elif mode == "refuse":
    sys.stdin.read()
    print(json.dumps({"status": "declined", "note": "fixture: editor declined"}))
else:
    os.execv(original[0], original + sys.argv[1:])
