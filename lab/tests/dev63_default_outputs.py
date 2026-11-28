"""Read dev63 parity surfaces from unmodified captured Core runs.

Import ``read_outputs`` on the current pair for comparisons. Run this module only
with the installed dev63/b15 pair to create a new, never-overwritten baseline.
"""
from io import StringIO
import json
import os
from pathlib import Path
import subprocess

from dev62_default_outputs import read_outputs as _read_outputs

SCENARIOS = {
    "agent-failed": "b14/agent-failed",
    "single-stage": "b15/insufficient-evidence",
    "focus-first": "b15/focus-first",
}


def read_outputs(run, *, include_panel=True):
    """Use the shared deterministic Owner inputs, actual CLI writer and panel UI."""
    from evolution_lab.cli.output import write_value

    outputs = _read_outputs(run)
    stream = StringIO()
    write_value(stream, outputs["campaign.json"], machine=False)
    outputs["cli-campaign.txt"] = stream.getvalue()
    if not include_panel:
        return outputs
    import evolution_lab.panel as panel

    tests = Path(__file__).resolve().parent
    rendered = subprocess.run(
        ["node", str(tests / "dev63_default_panel.mjs")],
        input=json.dumps(outputs["campaign.json"]), text=True,
        capture_output=True, timeout=30, check=True,
        env={**os.environ, "LAB_PANEL_STATIC": str(Path(panel.__file__).parent / "static"),
             "LAB_PANEL_FIXTURES": str(tests / "fixtures/panel")},
    )
    outputs["panel-independent.json"] = json.loads(rendered.stdout)
    return outputs


def main():
    from importlib.metadata import version
    import evolution
    import evolution_lab

    assert version("evolution-lab") == "0.0.3.dev63"
    assert version("evo-skills") == "2.0.0b15"
    assert "site-packages" in evolution_lab.__file__
    assert "site-packages" in evolution.__file__
    tests = Path(__file__).resolve().parent
    for name, fixture in SCENARIOS.items():
        destination = tests / "data/dev63-default-outputs" / name
        outputs = read_outputs(tests / "fixtures/panel" / fixture / "run")
        destination.mkdir(parents=True, exist_ok=True)
        for filename, value in outputs.items():
            assert not (destination / filename).exists(), "Never overwrite a captured baseline"
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            (destination / filename).write_text(text)


if __name__ == "__main__":
    main()
