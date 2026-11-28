---
name: evolution-lab-requirements
description: Read the Lab requirements catalogue that tells a builder what a Lab package needs.
---

# Lab requirements catalogue

Use this guide to read the catalogue of items a builder must settle before a
Lab package can be assembled. The catalogue is data, not prose: this guide
explains how to read it and which guide covers each group. It never restates an
item's title, reason, obligation or feeds; read those from the catalogue itself.

<a id="where"></a>
## Where the catalogue lives

The catalogue is `requirements.json`, next to this file, in the installed Lab
wheel. The same guides and catalogue ship with the Lab version that executes
the package, so read them from that installation rather than from a copy.

- Python: `evolution_lab.guides.requirements()` returns the parsed catalogue.
  It checks the closed schema, the `feeds` grammar and the `digest` on every
  read, and raises `ValueError` without a partial result on any mismatch.
- Command line: `lab guides requirements --json` prints the same object. It
  works offline and does not need a running service.
- Service: `GET /v1/guides/requirements` returns the same object.

Answers to the catalogue are written in `spec.answers.json` of the `connect` scenario,
schema `spec-answers/1`, which is generated from this catalogue; a new catalogue version
therefore changes the schema and the digests of `connect` and of the scenario catalogue.

The catalogue lists only items that feed a Lab package. Steps of the client's
own process (for example its own tests or review gates) are not catalogue
items; the client keeps them under its own id prefix.

<a id="document"></a>
## Document fields

| Field | Meaning |
|---|---|
| `schema` | Always `lab-requirements/1`. |
| `catalogue_version` | Integer that grows by one on any content change. It orders versions for people and logs. |
| `digest` | SHA-256 of the canonical catalogue without the `digest` key. Bind any confirmation to this value, not to the version. |
| `paths` | Exactly `["campaign", "evaluation"]`, in that order. |
| `items` | Items ordered by `id`. |

Canonical bytes for `digest`: sorted keys, `,`/`:` separators without spaces,
UTF-8, non-ASCII characters kept as is.

<a id="items"></a>
## Item fields

| Field | Meaning |
|---|---|
| `id` | Stable identifier `<group>.<name>`, for example `{req:eval.corpus}`. |
| `group` | The prefix before the dot. |
| `applies_to` | The paths on which the item belongs to the package. On any other path the client neither shows nor requires it. |
| `obligation` | `required` or `optional`; it holds on every path listed in `applies_to`. |
| `title`, `why` | Short text shown to a person. They are written in Russian because the client's requirements panel shows them to the user. |
| `feeds` | Where the answer ends up in the package (grammar below). |
| `answer_from` | A hint for the assistant: `code` (read the agent's code), `user` (ask the owner), `decision` (a choice to make together), `run` (observe a run). It is a hint, not a restriction. |
| `guide` | `<directory>/SKILL.md#<anchor>` inside these guides: the section that explains how to answer. |

<a id="paths"></a>
## Paths: `campaign` and `evaluation`

A path is the kind of package source, the same value as the Bundle's
`source_kind`:

- `campaign` — a package built from a native campaign manifest `/15` (or the opt-in `/16`). A fixed
  evaluation prepared from a campaign Bundle is also on this path.
- `evaluation` — a package built from a closed `lab-evaluation-input/1`, with
  no editor or reflector.

The client chooses the path before building the package; Lab does not infer
it. Changing the path changes the set of required items, so a confirmation
recorded for one path does not carry over to the other.

<a id="feeds"></a>
## `feeds` grammar

Each entry has one of four forms:

| Form | Meaning |
|---|---|
| `<document>:<JSON Pointer>` | A place in a document the builder writes. `<document>` is `manifest/15` or `manifest/16` (campaign manifest; `/16` is the opt-in family-15 form), `evaluation-input/1` (`lab-evaluation-input/1`), `launch/2` (`lab-target-launch/2`; the older `lab-target-launch/1` remains accepted for a custom launcher, but no catalogue item points at it), `bundle-request/2` (`lab-service-bundle-request/2`) or `case` (one corpus record). The pointer follows RFC 6901 over the serialized form, not over Python model names. For sections whose shape depends on a role kind (`/editor`, `/reflector`, `/scorer`) the pointer stops at the section. |
| `seam:<name>` | One of the four agent seams: `settings`, `session`, `trace`, `backends`. See the [target connector guide](../target-connector/SKILL.md). |
| `rule:<directory>/SKILL.md#<anchor>` | A requirement with no field in any Lab or core document. The build satisfies it; Lab does not check it. |
| `external:operator` | An input Lab neither receives nor checks. |

An item may list several `feeds`. They are the places where the answer
may end up, not a list to fill in full: put the answer where it fits your
agent.
Whether an item must be answered at all is its `obligation`, not the number
of feeds. A document pointer names the part of the document that holds the
answer, not always the exact field: RFC 6901 has no "field of every array
element", so `case:/private/script` means "in the scripted messages" (their
`fields`), as `manifest/15:/artifacts` means the whole section.

<a id="stability"></a>
## Stability of ids

- An id is never reused with a different meaning.
- Refining `title`, `why`, `feeds` or `guide` without changing the meaning
  produces a new `catalogue_version` and keeps the id.
- Removing an item or changing its `obligation` or `applies_to` produces a new
  `catalogue_version`. A client notices every change through `digest`.

Guides mention items only by id, in the form `{req:<id>}`; every mentioned id
exists in the catalogue of the same installation.

<a id="groups"></a>
## Which guide answers which group

| Group | Guide |
|---|---|
| `agent` | [Target connector](../target-connector/SKILL.md), except {req:agent.skills_layout} → Campaign package |
| `world` | [Target connector](../target-connector/SKILL.md), except {req:world.data} and {req:world.reference_time} → Evaluation inputs |
| `launch`, `target` | [Target connector](../target-connector/SKILL.md) — launch file, capacity, conformance mode |
| `eval` | [Evaluation inputs](../evaluation-inputs/SKILL.md), except {req:eval.test_isolation} → [Pitfalls](../pitfalls/SKILL.md) and {req:eval.metric_contract} → [Campaign package](../campaign-package/SKILL.md#metric-contract) |
| `scope`, `campaign` | [Campaign package](../campaign-package/SKILL.md) — manifest `/15`, the confidence gate, editable scope, roles, scale |
| — | [Diagnostics](../diagnostics/SKILL.md) — reading `check`, `conformance` and results |

The authoritative link for each item is its own `guide` field. Bundle and
preparation steps are described in the [preparation guide](../preparation/SKILL.md).
