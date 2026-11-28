"""Read dev64 parity surfaces using unmodified Core captures and fixed Owner inputs."""
from io import StringIO

from dev63_default_outputs import read_outputs as _read_outputs

SCENARIOS = {"task0": "b14/agent-finished", "focus-first": "b15/focus-first"}


def read_outputs(run, *, include_panel=True):
    from evolution_lab.cli.output import write_value

    outputs = _read_outputs(run, include_panel=include_panel)
    for name in ("campaign", "owner-report"):
        stream = StringIO()
        write_value(stream, outputs[name + ".json"], machine=False)
        outputs[name + ".txt"] = stream.getvalue()
    return outputs
