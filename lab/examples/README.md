# Examples

All examples are agent-neutral: they use synthetic agents and data — no LLM, no
keys, no real cases.

Each example is tied to a **version pair**: matching Lab and core wheels are
required. This checkout's current examples require the dev60/b12 pair;
its intended release identifier is `v0.0.3.dev60`, with delivery status and source provenance in
[RELEASES](../docs/RELEASES.md). Published dev59/b11 examples remain on
`v0.0.3.dev59`. Keep old runs and examples with their producing tag and wheels.

New here? [A tour of the examples](../../docs/guide/examples.md) explains what the
two current examples consist of, which file plays which role, and where to look
first.

| Example | What it shows | Pair | Status |
|---|---|---|---|
| [`owner/evaluation.py`](owner/README.md) | The full fixed-evaluation path through the Python Owner: Bundle → conformance → evaluation → report, launcher `lab-target-launch/2`, a minimal `TurnAgent`. Walked through in [Run your first evaluation](../../docs/guide/first-evaluation.md) | Lab dev60 / core 2.0.0b12, manifest `/15` | **Current pair.** Start here |
| [`owner/campaign.py`](owner/README.md) | A full offline campaign through the Python Owner on the core's sample editor, reflector and scorer: check → campaign → report → diff → export. Walked through in [Run a campaign](../../docs/guide/campaign.md) | Lab dev60 / core 2.0.0b12, manifest `/15` | **Current pair** |
| [`observations/`](observations/README.md) | Import local traces into observations, without running an agent | core-independent | Current |
| [`service/`](service/README.md) | Boundary of the service example. There is deliberately no script; the flow is in [CLI and local service](../../docs/guide/cli.md) | — | Reference |
| [`owner/offline.py`](owner/README.md) | A campaign through the Owner | dev7 / a8, manifest `/4` | Historical |
| [`preparation_s1/`](preparation_s1/README.md) | Preparing a campaign Bundle | dev7 / a8 | Historical |
| [`client/`](client/README.md) | The single-campaign client | dev7 / a8, manifest `/4` | Historical |
| [`campaign/`](campaign/README.md) | A campaign through the legacy runner | dev2 / a3 | Historical, does not run |
| [`standalone/`](standalone/README.md) | Standalone evaluation on the A3 preview | dev2 / a3 | Historical |

Historical examples are still useful for reading the call sequence, but the
current pair's examples use manifest `/15` (the Owner paths also accept `/16`), so their `/4` manifests will not pass
on it.
