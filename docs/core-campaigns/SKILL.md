---
name: core-campaigns
description: Use when running Evolution Core directly with evo tree, connecting a Target Service, integrating campaigns into CI, or inspecting and continuing a Core run directory.
---

# Core campaigns

Use this path when you own the Target Service, manifest and run directory. For pytest tasks, skill traces and skill versions, use [evolving-skills](../evolving-skills/SKILL.md).

## Start a campaign

1. Follow the [offline Core Quickstart](../core-quickstart.md) with the `evo-skills[tree]` package. `evo-target init` creates a scaffold with manifest `/15`. Run its `evo-target serve` in its own terminal, check the connection with `evo tree conformance`, then use `evo tree evolve` in a fresh run directory. The demo score applies to the scaffold.
2. For your agent, use the [backend and launcher example](../connecting-an-agent.md#start-from-the-working-backend-example) to adapt `TargetServer`. Load the exact supplied files from `LoadedTree.root` and isolate sessions. Match the manifest's artifact policy. Run [conformance](../cli.md#evo-tree-conformance) against the actual target before its first campaign.
3. Use the [manifest reference](../manifest-v2.md) for `/15` or `/16`. Set the seed tree, writable paths, corpus, scorer, roles and budgets. Keep the corpus and run directory outside the seed and role workspaces. Restrict role inputs to their allowed evidence. Propose the exact command and wait for approval before starting or resuming a campaign. For paid work, include the expected calls and cost cap.

## Measure and report in CI

Use `evo tree evaluate` to measure immutable seed and Champion trees. Give the before and after evaluations separate run directories. Use the same test cases, scorer, primary metric, repeats and Target settings. Preserve `group_id` boundaries between train and test. Run `evo tree evolve` on train and keep held-out results out of editing decisions. Check coverage before comparing scores. See [train and test](../running-campaigns.md#improve-on-train-and-measure-on-test).

Keep stdout JSON, stderr and the run directory. For `evo tree evolve`, exit `0` means completed. Check `result.rounds[].outcome` for `promoted`, then use `status.final_champion` and `status.campaign_outcome.champion_path` to locate the selected tree. Read the recorded reason for other exits. Keep secrets and private run evidence out of shared logs and commits. See [outcomes](../running-campaigns.md#read-the-outcome) and [exit codes](../cli.md#evo-tree-evolve).

## Inspect, resume or link

Start read-only with `evo tree watch --run-dir DIR --once`. Its exit `0` covers a snapshot or a message that the journal is still missing. Read the recorded state. Omit `--once` to follow live progress. Use `inspect_campaign(Path(DIR), verify=True)` for [verified inspection](../running-campaigns.md#inspect-a-run-without-running-it-again).

| State | Next action |
|---|---|
| Suspended | Repair the recorded cause. With unchanged bound inputs, propose the same `evo tree evolve` command and run directory. See [recovery](../running-campaigns.md#resume-after-a-failure). |
| Refused | Read stderr and every finding. For an authorizable finding, propose the exact `evo tree authorize` command Core prints, with the operator's reason and an offered effect. For a scorer change, check the [`rebaseline` and `close` effects](../cli.md#in-place-effects-on-family-7). Once the operator approves and records the authorization, repeat the original command to apply the effect. |
| Finished | To build on the final Champion, verify the saved result and create a [linked successor](../running-campaigns.md#continue-a-completed-campaign) with `linked_from` in a new manifest and a new run directory. Core decides lineage and baseline admission. |
