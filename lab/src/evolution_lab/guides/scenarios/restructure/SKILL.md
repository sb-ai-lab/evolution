---
name: evolution-lab-scenario-restructure
description: Port the observable behaviour of an existing agent onto a template step by step — analysis of the original, goal, brief for the builder (port spec and executor scope), build and verification — with a person confirming the map, the brief and the port.
---

# Scenario `restructure`: port the agent onto a template

The scenario carries the **observable behaviour** of an existing implementation — its public contract, answers
and their codes, side effects and channels — onto a template, not its code. It ends when a person accepts the
port: the confirmation `restructure.port_accepted` holds. It does not measure quality: the port gets its number
in its own `connect`, through the seam described in `evolution-lab-target-connector`.

Before you start:

- The materials root is not the source place itself; `lab project show <project>` prints both. It may lie
  inside the source place (for example the source at `.` and the materials in `lab/`): Lab leaves a materials
  root nested in a place out of that place's digest, so writing documents under `restructure/` does not change
  the source's digest and the `place:implementation:<source>` link stays fresh. A confirmation still drops when a
  document it attests or links changes — that is the chain described below. Lab compares the paths as written,
  without following symlinks, so both the source place and the materials root, with every directory on their way
  from the working copy, must be real directories: for each of the two,
  `realpath --relative-to="$(realpath <working-copy>)" <working-copy>/<path>` prints `<path>` as registered (a
  source place registered by an absolute path: `realpath <place>` prints `<place>`). If either does not, or if the materials root and the source
  place are the same directory, every document may change the source's digest: point the materials root at a real
  subdirectory first, `lab project set <project> --root <dir>`, `<dir>` relative to the working copy (for a
  symlink `lab -> state`, `--root state`); for the source place, ask the person to register the implementation at
  its real path. Do not drop the source link to get around this.
- The port exists. It is created before the scenario, from the implementation being ported:

```bash
lab project port <project> --from <source> --template <template-source>
```

- The scenario runs on the port (`--implementation <port>`); on the source implementation it is unavailable
  (`implementation.ported` is missing). Its documents live in `restructure/<port>/` of the materials root, where
  `<port>` is the port's implementation id. The schemas of its JSON documents are in `schemas/` next to this
  skill; print one with `lab scenarios schema restructure <name>` (`behavior-map/2`, `goal/2`, `port-spec/2`, `scope/1`, `verify/2`).
- The map, the goal and the brief are made of **fragments** — the top-level keys of their documents (for the
  brief, also the whole `scope.json`); the steps below name the fragments, their order and what goes into each.
  The template card — how the template works and which of its cells `evolve` improves — is `x-template` of the
  schema `goal/2`.
- Clients. The scenario does not depend on any client app; with a plain assistant and the CLI:
  - the assistant writes each document as one file at its path once every fragment is ready, and
    `lab project status` shows it `invalid` with the schema errors until it passes;
  - the person goes through the document fragment by fragment in conversation (or in the file) and says what is
    wrong; the assistant rewrites that fragment in the file;
  - the person answers `questions` in conversation, and the assistant records exactly the chosen option or the
    person's own words into `answer`, never a choice of its own: `{"option": <id>, "for": <digest>}` or
    `{"text": <words>, "for": <digest>}`, where `for` is `sha256:` plus the hex SHA-256 of the question without
    `answer` as UTF-8 JSON with sorted keys, separators `,` and `:` and non-ASCII kept
    (`json.dumps(q, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`); a question rewritten later loses
    its answer unless that digest is unchanged.

  A client app may give finer tools: one that writes and checks a single fragment and assembles the file itself, a
  screen where the person marks each section right or wrong and answers the questions, a build track. When it does,
  follow its own instructions on top of this skill and do not write the files it owns. The auto-evolve app, for
  example, gives `write_section(scenario="restructure", document="map"|"goal"|"brief", section=<fragment>,
  content=<value>)` (a fragment that does not pass the schema is refused with `section_invalid`; a brief fragment
  passes `basis` — the map and goal digests it was written from — and is refused with `basis_stale` when they
  changed) and `report_stage` / `note_template` for the build.
- Rules marked *(from creatorofuniverses/evolution-lab#2)* are taken, adapted to this scenario, from that pull
  request.
- Roles. The **assistant** talks with the person and writes the outputs of `analysis`, `goal` and `brief`. It reads
  the original in a copy of the source working tree without `.git`; `hide[]` of the scope hides paths from the
  executors, not from the assistant. The **builder** does the `work` steps (`build`, `verify`). The **reviewer**
  is a separate context: it accepts the output of every `work` step before that output is put at its final path,
  and it does the `claims_vs_wiring` layer of verification. Any assistant can be the builder; the reviewer is
  always a fresh context.
- `lab project status` shows each step as `done`, `stale`, `ready` or `blocked` with what is `missing`. Start
  there and return to it after every step:

```bash
lab project status <project> --implementation <port> --scenario restructure
```

- Follow the autonomy in the workspace's `AGENTS.md`. Steps marked `human_only` need the human's
  confirmation at `ask`. Use the MCP tool so Lab can ask. With the CLI, ask in the conversation
  and run the command after a yes. At `yolo`, run the requested steps.
- A step is done only when `lab project status` says `done` — not when a command exits 0.
- In every JSON document each item `id` is unique, and `uses[]` and `failures[].layer` name existing ids. The
  schemas cannot check this; the reviewer does.
- Each confirmation links every input consumed so far — the source, the map, the goal, the port spec — so a later
  change to any of them drops it. The commands below carry the links; do not shorten them. There are three
  confirmations: the map (`analysis-confirm`), the brief (`brief-confirm`, which also admits the build) and the
  port (`accept`). The goal has no confirmation of its own: the brief confirmation links it.
- Checkpoints. When a step becomes `done`, tag the port repository at its current `HEAD`, `<n>` being 1 plus the
  number of tags this step already has, the message being the digest of the step's file
  (`sha256sum restructure/<port>/<file>`):

```bash
git -C <port-place> tag -a restructure/<step>/<n> -m "<digest of the step's file>"
```

<a id="analysis"></a>
## Step `analysis` — map what the agent does

Goal: `restructure/<port>/behavior-map.json` (schema `behavior-map/2`) describes what the source agent does,
fragment by fragment, with where each item was read.

```bash
lab scenarios schema restructure behavior-map/2
```

The schema lists every fragment and its required fields.

The assistant does this step in conversation with the person, reading the copy of the
source working tree (without `.git`).

First build that copy: `<source-place>` is the source implementation's directory, `<view>` the directory of the copy.
Keep `<view>` outside the materials root, the port and the source place (simplest: outside the project's working
copy), and rebuild it from scratch every time, never in place. Do not remove the `hide[]` paths here: they hide
paths from the executors, not from the assistant. If the materials root lies inside the source place, remove it from
the copy (it holds this scenario's documents).

```bash
rm -rf -- <view> && mkdir -p <view>
rsync -a --safe-links --exclude=.git <source-place>/ <view>/
```

**Be compact.** The person reads the map item by item, and a client may draw each fragment as tidy blocks that long text breaks. For every fragment of the map and the goal: a label or a name is 1–3 words; an item's description is one line, up to about 100 characters; details go to `note` (where the field exists) or to `sources`, never into a label; no paragraphs. The architecture chain has 4–6 steps named by one word or a short phrase ("Транспорт", "Роутер", "Агент", "Ответ"); routing rule labels (input → where it leads) are short enough to sit above an arrow. Not like this: label «Транспорт · POST /api/parcels/chat — две точки входа…»; like this: label «Транспорт», note «POST /api/parcels/chat, две точки входа».

Language: every short note you write before a tool call, and everything the person reads (labels, descriptions, questions, notes in the fragments), is in the person's language, one line (a client may set the language; the field `agent_ru` is Russian whatever the language). In Russian texts the person sees, the restructured agent is «новый агент» and the original is «исходный агент»; never «порт», never «сборщик» (the executor is «builder»); in another language use the plain equivalents ("new agent", "original agent"), never "port".

Prepare the map **fragment by fragment**, in the order the person reads it: `problem`, `architecture`, `routing`, `scenarios`, `tools`, `knowledge`, `outcomes`, `assumptions`, `questions` — with a fragment tool of the client, write each as soon as it is ready; otherwise write `behavior-map.json` once all are ready (see "Clients"). Fix every schema error and write again. A written empty list is a valid answer ("no knowledge files found").

- `problem`: the job in one or two sentences, the channels, and `agent_ru` — a short Russian name for the header ("Агент доставки · вопросы о посылках, чат и голос").
- `architecture`: one-sentence summary, the key numbers, the chain, at most three notes on what matters for the port. `chain` is 4–6 steps transport → … → answer; a step `label` is a 1–3 word name, the longer explanation goes to its `note`. In `numbers`, `value` is the number only ("5", "≈1 200"), the explanation is the `label`.
- `routing`: rules "input → where it leads" (short `input` and `target`) and the risk in one sentence. Each rule's `note` starts with who decides it in the original: `модель:` when the original reaches it by calling a model (a classifier, a routing prompt, a value extracted from the user's text), even if code runs after that call; `код:` when code decides it without a model (a condition on a field, a fixed order). *(from the restructuring-demo port run, 2026-10-06)*
- `scenarios`: from the entry point through every branch — what is asked, how it ends (`outcomes`: `answered`,
  `off_topic`, `handoff`, `gave_up`, `error`), which tools and knowledge it uses (`uses`), and `port.decision`.
- `tools`: every call outside the agent; `side_effect: true` when the call changes something or sends something out.
- `knowledge`: prompts, documents, rules and data — what it is (`name`, `kind`), what it is for (`purpose`), where it
  lies (`location`).
- `outcomes`: the outcome codes of the original: code → what it means → the answer code.
- `assumptions`: what is inferred rather than read, numbered.
- `questions`: at most five questions **to the person**, each with options (mark the one you recommend) and the consequence of each option. Ask only about the goal, constraints and ambiguous behaviour; settle technical questions yourself from the source, the template and the project materials — the person need not supply code or an implementation recipe. Ask them only through `questions`, written with `answer: null`; the answer is the person's (see "Clients"). *(from creatorofuniverses/evolution-lab#2)*

A good item looks like this (one line per fragment; ids are unique within the fragment):

- `architecture.numbers[]`: `{"id": "n-lines", "label": "строк на Python", "value": "9 000", "sources": ["app"]}`
- `architecture.chain[]`: `{"id": "c-router", "label": "Роутер", "note": "граф из 12 цепочек", "sources": ["app/graph.py#build"]}`
- `routing.rules[]`: `{"id": "r-track", "input": "номер посылки", "target": "Где посылка", "note": "модель: классификатор запроса", "sources": ["app/router.py#route"]}`
- `scenarios[]`: `{"id": "s-where", "name": "Где посылка", "summary": "Находит посылку и называет статус.", "share": null, "outcomes": [{"kind": "answered", "text": "статус и дата"}], "uses": ["t-lookup"], "port": {"decision": "carry", "note": null}, "sources": ["app/flows/where.py#handle"]}`
- `tools[]`: `{"id": "t-lookup", "name": "lookup_parcel", "side_effect": false, "description": "Читает запись о посылке.", "sources": ["app/clients/tracking.py#lookup"]}`
- `knowledge[]`: `{"id": "k-statuses", "name": "Тексты статусов", "kind": "document", "purpose": "статусы для клиента", "location": "app/data/statuses.yaml", "sources": ["app/data/statuses.yaml"]}`
- `outcomes[]`: `{"id": "o-ok", "code": "OK", "meaning": "ответили", "answer_code": "200", "sources": ["app/codes.py:12-30"]}`
- `assumptions[]`: `{"id": "a1", "n": 1, "text": "Голос ходит теми же сценариями, что и чат.", "sources": ["app/voice.py#entry"]}`

Rules:

- Every map item says where it came from: `sources` is a list of paths in the source place with `#symbol` or `:lines`
  (`app/x.py#fn`, `app/x.py:10-40`). Scenarios, tools and knowledge must have it; routing rules, outcomes,
  architecture chain steps and numbers and assumptions have it as well whenever they were read off the source (it is
  optional for them — an item without one reads as inferred, so put an inference into `assumptions`). An item that
  reads as a fact but has no source looks invented. `problem` and `questions` carry no sources.
- What is inferred rather than read is a numbered `assumptions` entry; what the person must decide is a `questions`
  entry.
- `share` of dialogues only with a `basis` (traces, a corpus, the person's estimate); otherwise `null`.
- In every fragment each `id` is unique; the schema cannot check it.
- Do not design the port here; only `port.decision` per scenario: `carry`, `drop`, or `undecided` with a question
  for the person to choose.

An optional `BEHAVIOR_MAP.md` gives the same in prose; the JSON is the source of truth.

Done when: `restructure/<port>/behavior-map.json` exists and passes the schema `behavior-map/2` (it has every
fragment).

<a id="analysis-confirm"></a>
## Step `analysis-confirm` — confirm the behaviour map

**Needs the human's confirmation at `ask`.**

Goal: a person has gone through the map section by section — `architecture`, `routing`, `scenarios`, `tools`,
`knowledge`, `outcomes`, `assumptions` — each found right, or found wrong and talked over with the assistant; every
question in `questions` is answered (an empty `questions` needs nothing); each `undecided` scenario is settled
through a question or the person's word on its section — and confirms the map. The `problem` header has no section
of its own: the person corrects it through `architecture`.

A section found wrong goes back to the assistant, who rewrites that section. A rewritten section needs the person's
agreement again; a rewritten question keeps its answer only if it is unchanged. Then:

```bash
lab project confirm <project> --implementation <port> --claim restructure.map_confirmed --path restructure/<port>/behavior-map.json --link place:implementation:<source> --author <name>
```

Done when: the fact `restructure.map_confirmed` holds for `restructure/<port>/behavior-map.json`.

<a id="goal"></a>
## Step `goal` — agree on the goal

Goal: `restructure/<port>/goal.json` (schema `goal/2`) says, cell by cell of the template, what each part of the
original becomes in the port and when the port counts as done. The observable behaviour is carried, not the inner
logic: if the original goes to X, looks for Y and answers Z, the port does the same; an error code of the original is
the same code in the port.

```bash
lab scenarios schema restructure goal/2
```

Build the goal from the confirmed map; go back to the original only for a specific fact, not to re-read it. Stay compact, as on the map: a label or a name is 1–3 words, a description is one line up to about 100 characters, details go to `note` or `origin`, no paragraphs. Everything the person reads is in the person's language, with a one-line note before each tool call; the restructured agent is «новый агент», the original is «исходный агент», never «порт» (see "Language" above).

Read the template card first — `x-template` of this schema: how a turn passes through the template, and which cells
`evolve` improves later (`evolve`) and which stay code changed by hand (`code`). Prepare the goal fragment by
fragment, in the order `prompt`, `skills`, `tools`, `texts`, `handler`, `deps`, `modes`, `changes`, `questions` —
written as with the map (see "Clients").

One line of a good item per fragment (the rest of the fields: `lab scenarios schema restructure goal/2`):

- `prompt[]`: `{"id": "p-role", "title": "Роль", "text": "Ты отвечаешь клиентам о посылках.", "origin": "заменяет приветственную цепочку"}`
- `skills.links[]`: `{"id": "l-where", "from": [{"kind": "topic", "label": "Где посылка", "ref": "s-where"}], "method": "as_skill", "to": [{"kind": "skill", "label": "where"}], "note": null}`
- `tools.links[]`: `{"id": "l-lookup", "from": [{"kind": "call", "label": "lookup_parcel", "ref": "t-lookup"}], "method": "as_tool", "to": [{"kind": "tool", "label": "lookup_parcel"}], "note": null}`
- `texts[]`: `{"id": "x-handoff", "when": "посылка не найдена дважды", "file": "texts/handoff.md", "text": "Передаю вас оператору."}`

- `prompt` — the draft of the system prompt as blocks: `title`, `text`, and `origin` — what of the original it
  replaces ("replaces 12 classifiers").
- `skills` — `links` for the topics, prompts and ready skills of the original; `knowledge_links` — the knowledge
  items of the map, the same way.
- `tools` — `links` for every call of the original: it becomes a tool the model calls (`as_tool`), stays code in the
  handler or a dependency (`keep_code`, `to_handler`), or is not carried (`drop`).
- `texts` — the ready texts the agent answers with: `when` it is used, the `file` in the port and the `text`; keep
  the placeholders of the original as they are.
- `handler` — what runs in code around the model: `before` and `after` it, and `codes` — an answer code of the
  original → the code in the port. Only what the original decides in code goes here; a decision the map marks
  `модель:` stays with the model (see `rules` in step `brief`).
- `deps` — what a turn needs: `session` data, `clients` (each with its `offline` fake and its live `sandbox` or
  `null`), `state` kept between turns.
- `modes` — `offline` (fake backends, a scripted model) and `live` (real backends), each `included` or not; each
  included mode gets an end-to-end test in the brief. `ladder` — checkable criteria for acceptance, each naming the
  verification layer kind it becomes (`layer`, see step `verify`) or `null`, with its `threshold` (a share, 0 to 1)
  or `null`.
- `changes` — what the port does differently from the original on purpose, and why; an empty list means nothing.
- `questions` — as on the map: at most five, with options and consequences.

Rules:

- A scenario's `port.decision` is the person's decision in the confirmed map: copy it into the links (`drop` for a
  dropped scenario); do not decide it.
- `skills` and `tools` are links "before → method → after": `from` lists one or more items of the original (topic, prompt, ready skill, knowledge, call, system step), `to` — what they become in sylph; `method` is one of as_skill, merge (several → one), split (one → several), to_handler, to_prompt, as_is, as_tool, keep_code, drop (`to` empty). The template card is `x-template` of `goal/2`: which cells evolve improves and which are code.
- Rules keep the source conditions and ordering. Do not invent operations, side effects or new confirmation gates. An intended change of behaviour goes only to `changes` (what and why); everything not in `changes` is ported with the original conditions and order. *(from creatorofuniverses/evolution-lab#2)*

The person agrees the goal section by section: a section is found right, or found wrong (the assistant rewrites
it), or the person edits one of its text fields, which counts as agreement; `questions` are answered by the person;
`how` comes from the template card and is not agreed. The goal is edited freely until the brief is confirmed; after
that a change drops the brief confirmation.

Done when: `restructure/<port>/goal.json` exists and passes the schema `goal/2` (it has every fragment).

<a id="brief"></a>
## Step `brief` — write the brief for the builder

Goal: two documents from the confirmed map and the goal: `restructure/<port>/port-spec.json` (schema `port-spec/2`)
— the task, what is built in what order, the rules kept in code, how each requirement is tested and what `evolve`
may change; and `restructure/<port>/scope.json` (schema `scope/1`) — what the executors must not see.

```bash
lab scenarios schema restructure port-spec/2
lab scenarios schema restructure scope/1
```

Prepare the brief fragment by fragment, in the order the person reads it: `task`, `sees`, `stages`, `rules`,
`requirements`, `evolvable`, `bounds`, and `notes`. `sees` is the whole `scope.json` (see "The scope" below); every
other fragment is a key of `port-spec.json`. Written as with the map (see "Clients").

Write every brief fragment from the current map and goal. If either changed after a fragment was written, read
them again and rewrite that fragment (a client that tracks this refuses the stale write, as `basis_stale` in
auto-evolve).

The port spec:

- `task` — one or two sentences: "Port the agent … onto sylph".
- `stages` follow the template cells: contract → offline (prompt, skills, tools on fakes) → live dependencies → deterministic rules in the handler → the Lab seam (`src/lab_entry.py`, see step `build`); each with `done_when`; the seam stage is done when `src/lab_entry.py` is in the port and its `build(context)` constructs the agent from `SYLPH_MODEL` and `SYLPH_API_KEY`. The offline stage starts with **one** scenario carried from the real public entry point to the result on fakes, with the test and the expected result named in advance; only then widen coverage. Access and side-effect rules hold from that first path; the rules stage adds the rest. A successful import or a direct helper call does not prove the path. *(from creatorofuniverses/evolution-lab#2)*
- `cells` of a stage name the cells of the goal it builds (`prompt`, `skills`, `tools`, `texts`, `handler`, `deps`,
  `modes`, `seam`); each mode included in the goal gets its end-to-end test, named in the `done_when` of its stage.
- `rules` — rules the original decides in code, not left to the model (access, allowed transitions, required
  inputs, answer codes, the conditions and ordering its code enforces), in code, not only in the prompt; as the goal
  says, they keep the source conditions and ordering and add no operations, side effects or confirmation gates of
  their own. Code keeps what the original decided in code. When the original reaches a decision by calling a model —
  classifying or routing a request, choosing a branch, extracting a value from the user's text — the port leaves
  that decision to the model (prompt, skills, tools), even if deterministic code runs after it in the original: no
  keyword lists, regular expressions or lookup tables standing in for a model call. The map's routing rules say
  which side decides (`модель:` or `код:` in `note`). *(from the restructuring-demo port run, 2026-10-06)* Always include: "A scripted model tests the
  wiring, not the behaviour."
- `requirements` — one row per carried scenario of the map: requirement → the cell that holds it (`cell`) → test.
  A requirement with no test yet has `test` exactly `нет теста` — the person sees the gap marked; never leave it
  empty or write a test that does not exist.
- Plan checks of the observable contract: answers and codes, state transitions, errors, external calls, side effects, the request lifecycle. For each — the evidence that will cover it, or the gap named explicitly. A matching answer alone does not prove the behaviour; fake-backend evidence does not establish live-provider behaviour. *(from creatorofuniverses/evolution-lab#2)*
- `evolvable` — the files `evolve` may change later (the `evolve` cells of the template card); the template core is
  never among them.
- `bounds`: do not change `src/sylph/`; record the template version and its upstream commit in the report.
  `bounds.claims_check` — how the final "claimed but not wired" pass is done before the build is reported.
- `notes`: your margin remarks to the person, per section — where you have something to say ("two handler rules have no test yet — I suggest adding them to stage 4").

The template core is not changed. When the build is redone, carry `carry_to_brief` of the previous verification
and the findings of the previous review into the port spec if the person wants.

The scope (`sees`):

Hide what would let the executor copy answers instead of reading the agent: ready-made analyses of the agent,
benchmarks and corpora with reference answers, earlier ports, secrets. `hide[].path` is a literal path from the
root of the source place, not a pattern; a directory hides its whole subtree.

```
{"schema": "restructure-scope/1", "source": "<source>",
 "hide": [{"path": "docs/architecture.md", "reason": "ready-made analysis"},
          {"path": "benchmarks", "reason": "reference answers"}]}
```

A client fragment tool may take only `{"hide": [...]}` as the `content` of `sees` and add `schema` and `source`
itself (auto-evolve does); the file always has all three.

The source view. From `build` on, the builder and the reviewer read the original in a copy of the source
working tree without any `.git` (history would show what was hidden), without the `hide[]` paths and without the
materials root when it lies inside the source place (it holds this scenario's documents), kept outside
the materials root, outside the port and outside the source place (simplest: outside the project's working
copy), in a fresh context rooted in that copy. Build it in three steps and never
pass `hide[]` to rsync as filters. The view is rebuilt from scratch every time, never updated in place: stale files
or an old `.git` must not survive a rebuild.

```bash
rm -rf -- <view> && mkdir -p <view>
rsync -a --safe-links --exclude=.git <source-place>/ <view>/
# then, for every hide[].path, and for the materials root's path from the source place when it lies inside it,
# after checking that realpath("<view>/<path>") is inside <view>:
rm -rf -- "<view>/<path>"
```

Absolute symlinks and symlinks leading out of the tree are not copied. A path that resolves outside the view stops the build; a path that does not exist is reported to the person. The
view limits what the executor sees; it is not a security boundary, and Lab does not check it. An optional
`SCOPE.md` explains the choice in prose. The person confirms the brief in the next step.

Done when: both files exist and pass their schemas.

<a id="brief-confirm"></a>
## Step `brief-confirm` — confirm the brief and start the build

**Needs the human's confirmation at `ask`.**

Goal: a person has agreed the brief section by section — `task`, `sees` (what the builder will and will not see),
`stages`, `rules`, `requirements`, `evolvable`, `bounds` — each found right, or found wrong and rewritten by the
assistant, or edited by the person in its text fields; no section was written from an earlier map or goal; `notes`
are not agreed. Then the person confirms the brief as the assignment for the build. The confirmation attests `scope.json`: the executors' view is built from the
attested scope. It links the map, the goal and the port spec, so a change to any of them drops it.

```bash
lab project confirm <project> --implementation <port> --claim restructure.brief_confirmed --path restructure/<port>/scope.json --link file:restructure/<port>/behavior-map.json --link file:restructure/<port>/goal.json --link file:restructure/<port>/port-spec.json --link place:implementation:<source> --author <name>
```

The build starts on the person's instruction after this confirmation; it is long and paid.

Done when: the fact `restructure.brief_confirmed` holds for `restructure/<port>/scope.json`.

<a id="build"></a>
## Step `build` — build the port

Goal: the port's place holds the agent built by `port-spec.json`, and `restructure/<port>/BUILD_REPORT.md` says
what was built.
Inputs: the map, the goal, the port spec and the scope.

The builder writes code only in the port's place and reads the original only in the source view. Placeholders: `<port>` is the port's implementation id, `<source>` the source implementation id, `<port-place>` the port's directory, and "the source view" is the copy built as described in step `brief`. Stage by stage
as in the spec. `BUILD_REPORT.md` lists what is wired, the requirement table with its code and test columns filled,
remarks on the template, the template version with its upstream commit, and the port's `HEAD`. Before reporting,
commit everything in the port; the tree must be clean:

```bash
git -C <port-place> status --porcelain
git -C <port-place> rev-parse HEAD
```

The Lab seam. The template ships `src/lab_entry.py`: a module function `build(context)` that takes the model from
`context.env["SYLPH_MODEL"]` (and the optional `context.env["SYLPH_BASE_URL"]`) and the key from
`context.secrets["SYLPH_API_KEY"]`, and returns `sylph.evolution.agent_factory(...)` over the built application. These
names are a contract with whoever starts the port as a target (a client's chat with the new agent, a probe, the
port's `connect`): do not rename them. Check that the module is in the port and that `build` constructs the agent;
if the port was made from an older template without it, add it by the template's pattern
(`docs/application-turns.md`, «Привязка к Lab»). Never write `lab-target-entry.json` or a target environment (venv)
in the build: the person or the client prepares them from the port afterwards (`evolution-lab-target-connector`).

Report every stage of the brief as you go — started, done, one line — so the person can follow the build (in the
report, or with the client's tool: `report_stage(stage=<stage id>, state="started"|"done", note=<one line>)` in
auto-evolve); a stage you return to is reported started again. Notes about the template itself (what was awkward,
what is missing) go to the report's template remarks (`note_template(text)` in auto-evolve). The reviewer checks
the whole build once per cycle; a finding names its `stage` when it belongs to one.

The step is blocked until the port's place is available (`implementation.available`).

Done when: the file `restructure/<port>/BUILD_REPORT.md` exists.

<a id="verify"></a>
## Step `verify` — verify the behaviour is kept

Goal: `restructure/<port>/verify.json` (schema `verify/2`) lists the verification layers, each with its own status
and evidence, all run on one commit of the port.

```bash
lab scenarios schema restructure verify/2
```

Write the document to this schema; it lists every required field. Placeholders: `<port>` is the port's implementation id, `<source>` the source implementation id, `<port-place>` the port's directory, and "the source view" is the copy built as described in step `brief`.

Layers (`kind`): `offline_tests`, `contract_golden`, `lint_types`, `live_corpus`, `project_evaluation`,
`claims_vs_wiring`, `other`. Use the ones `goal.json` names in `modes.ladder`; `claims_vs_wiring` is always there.

- A layer proves its own thing and stands for no other. `passed`, `failed` and `partial` need evidence: a command,
  a file or a log. A matching answer alone does not prove the behaviour; fake-backend evidence does not establish
  live-provider behaviour. *(from creatorofuniverses/evolution-lab#2)*
- A decision the model makes (a routing rule marked `модель:` on the map) is tested with varied wording of the
  same request, not only with the map's own examples: a check that passes on the map's phrasing does not show the
  decision generalises. *(from the restructuring-demo port run, 2026-10-06)*
- The live layer gives honest `counts` (done, total, threshold); a person's decision to stop early goes to
  `decision`.
- Every failure gets `blame`: `env` (provider, timeout), `corpus` (the expectation was wrong) or `agent`.
- `project_evaluation` is `deferred`: the number comes from the port's own `connect`.
- `claims_vs_wiring` is done by the reviewer in a fresh context: the README and the report claim no more than is
  wired, the live path was exercised, access rules are not only in the prompt, no decision the map marks `модель:` is
  replaced by a keyword list or a regular expression, and the Lab seam is there —
  `src/lab_entry.py` exists and `build` constructs the agent (import it and call `build` with a stub context whose
  `env` has `SYLPH_MODEL` and whose `secrets` has `SYLPH_API_KEY`).
- `port_commit` is the commit all layers ran on, with a clean tree.
- `comparison` is before and after in size (lines, skills, tools, rules), never quality; `template_notes` are
  remarks on the template.

Besides the layers, write `accepting` — one sentence: what exactly the person accepts with this port — and `carry_to_brief`: the conclusions that would move into a refined brief if the person starts over (every gap and every failed or unmeasured check).

An optional `VERIFY.md` gives the same in prose.

Done when: `restructure/<port>/verify.json` exists and passes the schema `verify/2`.

<a id="accept"></a>
## Step `accept` — accept the port

**Needs the human's confirmation at `ask`.**

Goal: a person accepts the port as an implementation of the project on the evidence of `verify.json`.

First check that the verified commit is the one being accepted: `HEAD` must equal `port_commit` in `verify.json`
and the tree must be clean. Otherwise run `verify` again.

```bash
git -C <port-place> rev-parse HEAD
git -C <port-place> status --porcelain
lab project confirm <project> --implementation <port> --claim restructure.port_accepted --path restructure/<port>/verify.json --link file:restructure/<port>/goal.json --link file:restructure/<port>/scope.json --link place:implementation:<source> --link file:restructure/<port>/behavior-map.json --link file:restructure/<port>/port-spec.json --link file:restructure/<port>/BUILD_REPORT.md --link place:implementation:<port> --author <name> --note "<caveats, for example: live layer 2 of 20>"
```

This step is `lab project confirm`; `lab project accept` carries stale facts over to new bytes and is not this
step.

Any later change to the port's code, to the documents or to the source drops the acceptance.

Done when: the fact `restructure.port_accepted` holds for `restructure/<port>/verify.json`.

### Redoing the build, or a new port

Both are actions of the person, not steps.

- **Redo the build** in the same port: move `verify.json` (and its sidecar files, if any) and then `BUILD_REPORT.md`
  from `restructure/<port>/` to `restructure/<port>/redo/build-<n>/` (`<n>` is 1 plus the number of earlier redos),
  then reset the port to the tag `lab/port/created`. The map, the goal and the brief stay in place: the port spec is
  refined by editing it, which drops `brief-confirm` through its links, and the person confirms the brief again
  before the next build. Verification moves first, so an interrupted redo never leaves a port that can be accepted.
- **A new port**: run `lab project port` again; it gets a new id. The old port stays for comparison or is archived.
