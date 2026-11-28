---
name: evolution-lab-scenario-create-skill
description: Create one skill for an existing implementation, check it with example questions, and ask a person to accept it.
---

# Create a skill

Use this scenario with any assistant and the Lab CLI; consult `evolution-lab-target-connector` for the target connection. A skill is a folder or a single file in an existing implementation: many agents keep a skill as one file (for example, a `.md` or `.yaml` recipe in a domain folder). Discover where that agent looks for skills and its format, including whether a skill is a folder or one file; record these constraints in the brief rather than imposing a format. Only one run per implementation may be active. A run is active if any step document exists in `create-skill/<implementation>/`. Start another only after acceptance or abandonment.

The assistant discusses the task and writes the decide documents; the builder writes the skill; a reviewer in a fresh context checks the build and the answers. At `ask`, confirmations, revocations and acceptance of changes need the human's approval. Use the MCP tool so Lab can ask. With the CLI, ask in the conversation and run the command after a yes. At `yolo`, run the actions the human requests. At the beginning and after every step run:

```sh
lab project status <project> --implementation <implementation> --scenario create-skill
```

Before starting a new run, move all top-level run files and `redo/` to `create-skill/<implementation>/done/<skill.name>/` after acceptance, or to `abandoned/<n>/` after abandonment. Do not move the existing `done/` or `abandoned/` directories into themselves. Find the old confirmation IDs with `lab --json project confirmations <project>` and select records whose `implementation` is the current implementation. The person revokes both previous confirmations, if present:

```sh
lab project revoke-confirmation <project> <brief-confirmation-id> --reason "archived: <skill.name>"
lab project revoke-confirmation <project> <accept-confirmation-id> --reason "archived: <skill.name>"
lab project confirmations <project> --revoked
```

Without revocation old confirmations become stale. `lab project accept <project> --implementation <implementation> --reason "inspect"` previews changes and can carry a previous brief confirmation to new files if the person answers yes; decline the prompt when inspecting. Revoked confirmations and archived files preserve history. Until archive, `create_skill.accepted` refers to the last accepted skill for this implementation. Never use `lab project accept` as a substitute for a new human confirmation.

<a id="task"></a>
## Task

**Goal:** Record the person's task. Read the supplied files, including text, spreadsheets, JSON, JSONL and Markdown where possible. Tell the person about every unread input and its reason before the brief. Write `create-skill/<implementation>/task.json` in the `task/1` schema with the request in their words.

```sh
lab scenarios schema create-skill task/1
```

Done when: the task and every input, including unread inputs and their notes, are recorded.

<a id="scenarios"></a>
## Scenarios

**Goal:** Agree on situations in which the skill should help. Propose scenarios, each with example questions, expected observable behavior and needed tools. Ask the person to include or exclude each; unanswered is `undecided`, never assumed consent. Save `create-skill/<implementation>/scenarios.json`.

```sh
lab scenarios schema create-skill scenarios/1
```

Done when: each scenario has at least one example and the person's decision is recorded.

<a id="tools"></a>
## Tools

**Goal:** Record only tools already available to the agent, with sources in the implementation or its connection description. Ask the person which are needed; do not invent or add tools. Save `create-skill/<implementation>/tools.json`.

```sh
lab scenarios schema create-skill tools/1
```

Done when: the existing tools and the person's decisions are documented.

<a id="brief"></a>
## Brief

**Goal:** Write `create-skill/<implementation>/brief.json` for a new skill: a folder or one file. Include at least one included scenario, its example questions and expected behavior, only needed existing tools, constraints and check plan. The skill path is relative to the implementation place, does not exist yet and does not replace an existing skill. Its name must be unique in `done/`. Discover the agent's skill conventions and record them in constraints; the path names the folder or the file itself; show the path and brief to the person for correction. Check whether the path is covered by a Bundle root when an implementation has one.

```sh
lab scenarios schema create-skill brief/1
lab project tree <project> --implementation <implementation> --path <skill-path> --json
```

Done when: the person has seen the brief and chosen the path, scenarios, tools and expected behavior.

<a id="brief-confirm"></a>
## Confirm the brief

**Needs the human's confirmation at `ask`.** **Goal:** Obtain explicit human confirmation before building. Record the person's decision with:

```sh
lab project confirm <project> --implementation <implementation> --claim create_skill.brief_confirmed --path create-skill/<implementation>/brief.json --link file:create-skill/<implementation>/task.json --link file:create-skill/<implementation>/scenarios.json --link file:create-skill/<implementation>/tools.json --author <name>
```

Done when: the person confirms the brief and the status shows the confirmation.

<a id="build"></a>
## Build

**Goal:** Build only the new skill: inside its folder, or only its one file. Before the builder first writes, save the full listing. Compute SHA-256 of the saved `place-before.json` bytes (this is the snapshot file digest, not the listing’s `digest` field):

```sh
lab project tree <project> --implementation <implementation> --path . --json
lab scenarios schema create-skill build/1
```

Save that JSON output to `<materials-root>/create-skill/<implementation>/place-before.json`. Before the builder starts, record `sha256:<hex>` of those exact bytes outside the builder’s control (for example, in the human's record). Before using the snapshot, the reviewer must compute a fresh SHA-256 of `place-before.json` and compare it with the independently recorded hash; do not proceed on a mismatch. The reviewer performs this check without requiring an existing `build.json`: after approval, write the verified hash into `build.json.place_before` when creating that document. On a return to the builder, repeat the comparison against the original independent hash; the previous report is archived, and the new report is created only after approval. The parent directory of the new skill must already exist before the snapshot: choose an existing parent or have the human create it before taking the snapshot. The builder writes only the skill folder or the skill file. The reviewer writes `create-skill/<implementation>/build.json` after approval.

The reviewer in a fresh context compares the current `tree --path . --json` with the unchanged snapshot. Derive the scenario directory’s path relative to the implementation place when it is visible there. Ignore the skill (its folder and everything under it, or exactly its one file) and these exact **scenario-owned comparison exceptions** only: `create-skill/<implementation>/place-before.json`, `build.json`, `check.json`, `redo/build-<n>/build.json`, `redo/build-<n>/check.json`, and `redo/check-<n>/check.json` (the latter five paths are relative to the same scenario directory; `<n>` is a positive integer). An exception applies only to regular files or absence, never a replacement by a symlink, directory or other type. For directories, ignore only an added directory that is a necessary ancestor of one of those actual exception files; compare every pre-existing directory’s type and executable bits, and report removals. Compare every other entry, including `task.json`, `scenarios.json`, `tools.json`, `brief.json` and other files even inside `redo/`: never exclude the whole scenario directory. Independently list additions, removals and changes of `(type, executable, sha256, target)` outside the skill and these narrow exceptions in `outside_changes`; acceptance requires an empty list.

This rule works when the materials root equals the place. When it is nested, Lab cuts it out of the place listing; when separate, its files are outside the place. Project exclusions and common ADR-0017 excluded names are not covered by this boundary, so tell the person. Check that files cover every brief scenario and refer only to its tools. Blocker or major findings require another builder pass. Keep the original snapshot and independently recorded hash on every return to the builder.

Done when: the reviewer approves, independently verifies no outside changes, and the build document validates.

<a id="check"></a>
## Check

**Goal:** Ask the agreed example questions on the exact skill bytes and report coverage; this is not a quality score. Each check, including after human edits, starts from a fresh Bundle:

1. Close any open probe and stop the live target, if present: `lab agent stop <target-id> --generation <generation> --wait`.
2. Read `lab project tree <project> --implementation <implementation> --path <skill-path> --json`. Save its digest as `D`; require `bundle_covers: true`. If false, write `not_run` / `not_in_bundle` and tell the person the skill is outside Bundle roots. If a Bundle request or target cannot be prepared, record `not_run` / `not_connected`.
3. Build `lab project bundle <project> --implementation <implementation>` and start a target using that new digest with `lab project target <project> --implementation <implementation> --bundle <bundle-digest>`. Record the Bundle digest and target start time. Do not use an older Bundle or a target started before the last change.
4. Read the skill digest again. If it differs from `D`, start the procedure again before asking questions.
5. Start a probe with `lab probe start <project> <implementation> --max-turns <number-of-example-questions>`. Ask each example in one turn with `lab probe say <session> "<question>"`; before each next example use `lab probe reset <session>` and use the new session ID. The ceiling applies to the reset chain. Close the last session with `lab probe close <session>`: an open probe blocks target operations.
6. The reviewer assesses each response against its expected behavior, records session/turn IDs and a verdict. `agent_outcome` `infrastructure` or `error` is `not_checked` with a note, not `failed`. A scenario is failed if any example failed, otherwise not_checked if any was not checked, otherwise passed. Show the person each verdict and "N of M checked"; record disagreement in `human`.

On interruption preserve completed verdicts only while continuing on the same live target and the skill digest is still `D`; a new probe session on that target is allowed, with new session IDs recorded. If the target falls or otherwise needs a restart, repeat steps 1–4 and start over with no prior verdicts, even when the skill and Bundle digests are unchanged: a restarted target has a new `started_at`, and the single `target` in the check must describe every recorded answer. If `D` changes, repeat steps 1–4 and start over with no prior verdicts too. If unable to finish, use `status: partial`, `reason: interrupted` (even if no example was checked); a partial report may retain answers from its one original target if no new target was started. If no connection is possible, write `status: not_run`, `reason: not_connected` and mark all verdicts `not_checked`. Write `create-skill/<implementation>/check.json` with `skill_digest: D` and target details for checked/partial; use null digest and target for not_run.

```sh
lab scenarios schema create-skill check/1
```

Done when: the check document validates and the person sees the verdicts and coverage, including any interruption or missing connection.

<a id="accept"></a>
## Accept

**Needs the human's confirmation at `ask`.** **Goal:** Let the person decide. Re-read the skill digest with `lab project tree`. If a checked or partial result has a different `check.skill_digest`, either check again or, at the person's direction, move the previous check to `redo/check-<n>/check.json`, list it in `previous`, and write a new `not_run` / `changed_after_check` document. Record `closed` according to the person's decision, including an explicitly unclosed scenario. Record the person's decision with:

```sh
lab project confirm <project> --implementation <implementation> --claim create_skill.accepted --path create-skill/<implementation>/check.json --link file:create-skill/<implementation>/task.json --link file:create-skill/<implementation>/scenarios.json --link file:create-skill/<implementation>/tools.json --link file:create-skill/<implementation>/brief.json --link file:create-skill/<implementation>/build.json --link tree:implementation:<implementation>:<skill-path> --author <name> --note "<coverage and caveats>"
```

For `not_run`, the note states "not checked with questions"; for `partial`, "partially checked: N of M examples". A person may accept with an unclosed scenario, or return the work.

Done when: the human has decided and, if accepted, the confirmation is recorded with the skill's `tree` link and honest coverage.

## Return to builder

This is a human action, not a scenario step. Move `build.json` and `check.json` to `create-skill/<implementation>/redo/build-<n>/` and give the builder the person's comments. The builder changes the skill in place; keep `place-before.json` from before the first build for the next review.
