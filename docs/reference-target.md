# Reference Target

Evolution v2 evaluates a Candidate tree through a Target Service that speaks the `/evolution/v2` wire. The package ships a reference implementation of that service as the console script `evo-target`, so an operator can exercise the native v2 tree commands before writing a service of their own, and a team building its own target can run the reference beside it.

The reference target is a demonstration target. It loads a Candidate tree under one allowed root, attests the tree's Candidate and dependency digests over the bytes it loads, and answers every turn with the first line of `skills/SKILL.md` followed by the message. Each reply carries `provenance` (see [Replies and events](target-server.md#replies-and-events)): `{"used": ["skills/SKILL.md"]}` when the instance read that file and `{"used": []}` otherwise; it declares no `components()`. It contains no model, keeps every instance and session in memory until it is released, and is sized for one operator's runs on loopback. It is not a production service.

For installation, expected output and troubleshooting, start with [Your first campaign](../QUICKSTART.md). This page is the reference for the generated manifest and programs. To replace the demo with your agent, use [Connecting an agent](connecting-an-agent.md).

## Quickstart

```bash
evo-target init ./demo
evo-target serve ./demo/manifest.json
```

In a second terminal:

```bash
evo tree conformance --manifest ./demo/manifest.json
evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-1
```

`init` creates seven files under `./demo` — `manifest.json`, `seed/skills/SKILL.md`, `seed/uv.lock`, `corpus.jsonl`, `scorer/score.py`, `editor/edit.py` and `reflector/reflect.py` — and refuses a non-empty directory. `serve` prints one JSON line when the target is listening and blocks until `SIGINT` or `SIGTERM`. `evo tree conformance` runs both conformance suites against the running target and prints its report, and is the sensible thing to run once before the first campaign. `evo tree evolve` runs the sample campaign against the served target and prints one JSON document. If port 8765 is already in use, `serve` exits `1` with `cannot bind`, and the remedy is to edit `target.base_url` in the manifest. `evo tree evolve` needs a `python3` on the `PATH` of the process that runs it, because the sample declares `python3` as the interpreter of all three programs and the binding resolves it against that `PATH`; without one the bindings are refused with `scorer_invalid at scorer.argv`.

## The sample manifest

`init` writes this `evolution-v2-manifest/15` document with the recommended `/15` values: a `statistical` gate with `stability: stable`, `focus.margin: 0` and `fresh_parent: false`; `round_stop: first_accepted`, so a round stops at its first accepted finalist; `baseline_refresh_after_rounds: 3`, so the Champion is measured again on the full corpus once three rounds have closed on the same stored measurement; a top-level `grouper` of kind `fields` over two observation features, so the sample needs no provider credential; the `scorer.metric_contract` the reference target satisfies, because every reply carries an `assistant_message` whose `/content` is a non-empty string; and `role_visibility` with empty `readable_paths`, so the Reflector and editor see only the `writable_paths` files of the tree. It also declares `campaign.budget.max_failed_turns: 1`, `campaign.call_retries: {max_attempts: 4, turn_max_attempts: 1}` (four attempts for every target call, and a failed agent turn that is never resent, which is the sound declaration for an agent whose session that turn may have changed) and `reflector.max_consecutive_failures: 2` (see [Reflector and Editor resilience](manifest-v2.md#reflector-and-editor-resilience-family-10)), and neither `campaign.sample_retries` (default `1`) nor `linked_from`. Its roles stay `subprocess`, with no `diagnostics` output and `reflector.families: {"kind": "none"}`. A `/15` document makes the run a family-14 campaign. An infrastructure failure, or a role failure not proven to be about its content, suspends the campaign instead of closing it, and the same `evo tree evolve` continues it:

```json
{
  "schema": "evolution-v2-manifest/15",
  "seed_root": "seed",
  "target": {
    "base_url": "http://127.0.0.1:8765",
    "seed_mode": "exact"
  },
  "artifacts": {
    "excludes": [
      "build/**"
    ],
    "dependency_paths": [
      "uv.lock"
    ],
    "writable_paths": [
      "skills/**"
    ]
  },
  "role_visibility": {
    "version": "role_visibility/1",
    "readable_paths": []
  },
  "campaign": {
    "rounds": 2,
    "attempts_per_round": 1,
    "parallel_attempts": 1,
    "evaluation": {
      "repeats_per_question": 3
    },
    "round_stop": "first_accepted",
    "baseline_refresh_after_rounds": 3,
    "indeterminate": {
      "target": {
        "max_count": 2,
        "max_rate": "0.5"
      },
      "environment": {
        "max_count": 2,
        "max_rate": "0.5"
      },
      "scorer": {
        "max_count": 2,
        "max_rate": "0.5"
      },
      "rollout": {
        "max_count": 2,
        "max_rate": "0.5"
      }
    },
    "budget": {
      "max_turns": 2,
      "timeout_seconds": 30,
      "max_failed_turns": 1
    },
    "call_retries": {
      "max_attempts": 4,
      "turn_max_attempts": 1
    },
    "max_concurrency": 2,
    "max_scorer_concurrency": 2,
    "metric": {
      "primary": "score"
    },
    "random_seed": 7,
    "editor_timeout_seconds": 60,
    "editor_context": null,
    "reflection_timeout_seconds": 60,
    "gate": {
      "mode": "statistical",
      "stability": "stable",
      "focus": {
        "confidence": 0.9,
        "margin": 0
      },
      "causal": {
        "confidence": 0.9,
        "tolerance": "measured_noise"
      },
      "provenance": {
        "kind": "reported"
      },
      "fresh_parent": false,
      "global": "point"
    },
    "attempt_feedback": "sequential"
  },
  "environment": {
    "kind": "static",
    "input_text_key": "text"
  },
  "scorer": {
    "argv": [
      "python3",
      "score.py"
    ],
    "cwd": "scorer",
    "timeout_seconds": 30,
    "dependency_paths": [],
    "allowed_env": [],
    "metric_contract": {
      "version": "metric_contract/1",
      "kind": "pointwise",
      "target_fields": [
        {
          "event": "assistant_message",
          "pointer": "/content",
          "value": "nonempty_string"
        }
      ]
    }
  },
  "editor": {
    "kind": "subprocess",
    "argv": [
      "python3",
      "edit.py"
    ],
    "cwd": "editor",
    "dependency_paths": [],
    "allowed_env": []
  },
  "grouper": {
    "kind": "fields",
    "features": [
      {
        "name": "failed_criteria",
        "source": "observation",
        "path": "/failed_criteria",
        "description": "Failed criteria."
      },
      {
        "name": "terminal",
        "source": "observation",
        "path": "/terminal",
        "description": "Terminal observation status."
      }
    ]
  },
  "reflector": {
    "kind": "subprocess",
    "argv": [
      "python3",
      "reflect.py"
    ],
    "cwd": "reflector",
    "dependency_paths": [],
    "allowed_env": [],
    "environment_docs": null,
    "probe": null,
    "max_consecutive_failures": 2,
    "selection": {
      "focus_cases": 16,
      "representatives_per_case": 2,
      "context_page_size": 32
    },
    "families": {
      "kind": "none"
    }
  },
  "corpus": {
    "path": "corpus.jsonl"
  }
}
```

The port is fixed so the manifest is complete before the target starts and the same file serves `evo-target serve`, `evo tree conformance` and `evo tree evolve`.

`campaign` runs two rounds of one Attempt each, with `parallel_attempts: 1` and `repeats_per_question: 3`: small enough that the packaged sample editor can reach a promotion in round 1 and the promoted Champion can close round 2 as the clean round of the walkthrough below. Two rounds never reach the baseline refresh. There is no alpha. See [Manifest v2](manifest-v2.md#stable-family-keys-manifest-15) for the key rules.

`environment` is `{"kind": "static", "input_text_key": "text"}`, so each case's `public.text` becomes the one turn the target sees. See [Manifest v2](manifest-v2.md#environment) for the key rules.

`scorer`, `editor` and `reflector` all declare `argv: ["python3", <script>]`: `python3` is resolved to one absolute executable against the child environment's `PATH` at bind, and each program's `cwd` (`scorer`, `editor` and `reflector` respectively) resolves against the manifest's directory, beside `seed` rather than inside it, so the sealed program files sit outside the seed tree the campaign rewrites. See [Manifest v2](manifest-v2.md#scorer), [Manifest v2](manifest-v2.md#editor) and [Manifest v2](manifest-v2.md#reflector) for the key rules.

`grouper` is a `fields` Grouper over the authenticated `failed_criteria` and `terminal` headers, so the sample needs no provider. `reflector` has no environment documentation or probe in the sample; its selection takes up to 16 cases with two representatives each, and families are disabled. `scorer.metric_contract` declares the one field the sample scorer depends on, a non-empty `content` in the last `assistant_message` event, which the reference target emits. `role_visibility.readable_paths` is `[]`, so the Reflector and the editor see only the writable `skills/**`. `campaign.attempt_feedback: "sequential"` carries earlier closed attempts into later adaptive briefs.

`corpus` points at `corpus.jsonl`: two cases, `q1` and `q2`, both in the group `g` and both declaring `oracle.critical: true`, so a scorer failure on either vetoes the sample. See [Manifest v2](manifest-v2.md#corpus) for the key rules.

## The sample campaign

`evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-1` runs the two-round sample campaign the manifest declares, against `scorer/score.py`, `editor/edit.py`, `reflector/reflect.py` and `corpus.jsonl`, each copied byte-for-byte from the package:

```python
"""Sample scorer: 1.0 when the target answered with the improved line.

Reads the one-object scorer document (`case_id`, `public`, `oracle`, `view`)
on stdin and writes `{"metrics": {"score": s}, "critical_pass": true,
"verdicts": [...]}` on stdout, `s` being 1.0 when the serialized trajectory
view contains `IMPROVED` and 0.0 otherwise. Every corpus row declares
`oracle.critical`, so `critical_pass` is required for exactly these cases.
The one verdict, `improved`, is the Reflector's failure channel: it passes
exactly when the score is 1.0, so the seed's samples carry a failed criterion
and the sample campaign has a failing cohort to reflect on. A sample, not a
recommended scorer.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    score = 1.0 if "IMPROVED" in json.dumps(payload["view"]) else 0.0
    sys.stdout.write(
        json.dumps(
            {
                "metrics": {"score": score},
                "critical_pass": True,
                "verdicts": [{"criterion": "improved", "passed": score == 1.0}],
            }
        )
    )


if __name__ == "__main__":
    main()
```

```python
"""Sample editor: rewrite the first line of the Candidate's skills/SKILL.md.

Reads the `edit_request/1` document on stdin (its `brief` field is the
`edit_brief/5` or `/7` brief), replaces the first line of
`<workspace>/skills/SKILL.md` with `IMPROVED: reference skill` and prints
`{"status": "edited", "note": "first line improved"}`. On a Candidate whose
first line already is that line the bytes are unchanged and the campaign
records the attempt as `editor_no_change`. A sample, not a recommended
editor. The sample is written for `edit_brief/5` and `edit_brief/7` and refuses any other
version rather than editing blind.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

IMPROVED_LINE = "IMPROVED: reference skill"


def main() -> None:
    request = json.load(sys.stdin)
    brief = request.get("brief") if isinstance(request, dict) else None
    version = brief.get("version") if isinstance(brief, dict) else None
    if version not in {"edit_brief/5", "edit_brief/7"}:
        sys.stderr.write(f"edit.py: expected edit_brief/5 or edit_brief/7, got {version!r}\n")
        raise SystemExit(2)
    skill = Path(request["workspace"]) / "skills" / "SKILL.md"
    lines = skill.read_text(encoding="utf-8").splitlines()
    lines[0:1] = [IMPROVED_LINE]
    skill.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": "edited", "note": "first line improved"}))


if __name__ == "__main__":
    main()
```

```python
"""Sample Reflector: one diagnosis group per portion cohort that has an exemplar.

Reads the `reflect_request/2` document on stdin and prints
`{"status": ..., "groups": [...], "note": "sample reflector"}`. For every
cohort of the request's `evidence.portion` that carries an exemplar it claims
one group: the seed's first line does not carry what the scorer looks for,
citing that exemplar, claiming the cohort's case and naming the seed's one
skill file. It uses the supplied `list_evidence` and `read_evidence` helpers
to confirm the selected observations before it makes those claims. An empty
portion — no failing cohort to account for — is `declined` with no groups.
A sample, not a recommended Reflector: its hypothesis is the one the sample
campaign is built around.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence

REQUEST_VERSION = "reflect_request/2"
ROOT_CAUSE = "the skill's first line does not carry the improvement the scorer looks for"
SUGGESTED_FIX = "rewrite the first line of skills/SKILL.md"
TARGET_PATH = "skills/SKILL.md"


def _reader_call(
    readers: object, operation: str, arguments: dict[str, object]
) -> Mapping[str, object] | None:
    """Run one sealed helper command and accept only its one JSON object result."""

    if not isinstance(readers, Mapping):
        return None
    argv = readers.get(operation)
    if (
        not isinstance(argv, Sequence)
        or isinstance(argv, (str, bytes))
        or not all(isinstance(part, str) for part in argv)
    ):
        return None
    try:
        completed = subprocess.run(
            [*argv, json.dumps(arguments, separators=(",", ":"))],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        result = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not isinstance(result, Mapping) or "error" in result:
        return None
    return result


def _selected_observations(request: Mapping[str, object]) -> set[str] | None:
    """Confirm each selected exemplar through the operation-local helpers."""

    evidence = request.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    portion = evidence.get("portion")
    if not isinstance(portion, list):
        return None
    selected = {
        exemplar
        for cohort in portion
        if isinstance(cohort, Mapping)
        for exemplars in (cohort.get("exemplars"),)
        if isinstance(exemplars, list)
        for exemplar in exemplars[:1]
        if isinstance(exemplar, str)
    }
    if not selected:
        return set()
    readers = request.get("readers")
    catalog = _reader_call(readers, "list_evidence", {})
    if catalog is None or not isinstance(catalog.get("entries"), list):
        return None
    for evidence_id in selected:
        if (
            _reader_call(readers, "read_evidence", {"evidence_id": evidence_id, "offset": 0})
            is None
        ):
            return None
    return selected


def main() -> None:
    request = json.load(sys.stdin)
    version = request.get("version") if isinstance(request, dict) else None
    if version != REQUEST_VERSION:
        sys.stderr.write(f"reflect.py: expected {REQUEST_VERSION}, got {version!r}\n")
        raise SystemExit(2)
    selected = _selected_observations(request)
    if selected is None:
        print(json.dumps({"status": "failed", "groups": [], "note": "reader unavailable"}))
        return
    evidence = request.get("evidence")
    assert isinstance(evidence, Mapping)
    portion = evidence.get("portion")
    assert isinstance(portion, list)
    groups = [
        {
            "root_cause": ROOT_CAUSE,
            "suggested_fix": SUGGESTED_FIX,
            "evidence_ids": [cohort["exemplars"][0]],
            "affected_cases": [cohort["case_id"]],
            "target_paths": [TARGET_PATH],
            "counterevidence": "No passing observation was selected for this cohort.",
            "regression_risks": "Keep the existing response format while changing the first line.",
            "why_this_layer": "The observed failure is caused by the editable skill text.",
        }
        for cohort in portion
        if isinstance(cohort, Mapping)
        and isinstance(cohort.get("exemplars"), list)
        and cohort["exemplars"]
        and isinstance(cohort["exemplars"][0], str)
        and cohort["exemplars"][0] in selected
        and isinstance(cohort.get("case_id"), str)
    ]
    status = "reflected" if groups else "declined"
    print(json.dumps({"status": status, "groups": groups, "note": "sample reflector"}))


if __name__ == "__main__":
    main()
```

```jsonl
{"case_id": "q1", "group_id": "g", "stratum": "s", "public": {"text": "question one"}, "oracle": {"critical": true}}
{"case_id": "q2", "group_id": "g", "stratum": "s", "public": {"text": "question two"}, "oracle": {"critical": true}}
```

The seed Champion scores `0.0` on both `q1` and `q2`, since its `skills/SKILL.md` first line does not contain `IMPROVED`, and the scorer's one verdict `improved` fails on every repeat. Before the baseline, the connection preflight runs one rollout of the seed Champion on `q1`, finds the `assistant_message` content the contract declares and a scored primary metric, and lets measurement start. Round 1 therefore has a failing cohort: the Reflector runs first, returns one group per cohort — each citing that cohort's exemplar repeat and naming `skills/SKILL.md` — and the round's gate scope is the cases those groups claim. Round 1's one Attempt then runs the editor, which rewrites the first line and scores `1.0` on both cases. Every repeat scores `1.0` against the baseline's `0.0`, so the focus comparison has no within-case variation and is decided on its point: the Attempt closes `finalist`. Its final measurement, `m-r01-final-01-a01`, measures the finalist again on both cases. Under `stable` it is compared with the stored baseline `m-r00-champion`, the Champion is not measured again, and with `global: point` the finalist is accepted and promoted.

The sample is deterministic, so its report carries `exact_contrast`: no within-case variation was observed in 3 repeats. That is not a claim that an agent is deterministic, and a real agent's comparisons usually vary. Every `stable` result also carries `champion_not_contemporaneous`, and with `fresh_parent: false` the Attempt carries `focus_selection_bias`. The sample's `focus.margin: 0` is the recommended value; a negative margin can promote an edit that made the focus worse.

Round 2 is the clean round. Its baseline is the promoted finalist's final measurement, `m-r01-final-01-a01`; the promoted Champion answers with the improved line, so every `improved` verdict passes, the deterministic grouping finds no failing cohort at all, and every repeat was readable. The round closes `clean` without running a reflection and without opening an Attempt: there is nothing to diagnose, so nothing is spent trying. The campaign then completes with `finished`/`completed`. A second campaign in a fresh run directory prints the same outcome; here it is with `--quiet`:

```bash
evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-2 --quiet
```

```json
{
  "terminal": "finished",
  "finished": {
    "continuation": {
      "kind": "linked_run",
      "reason": "rounds_exhausted"
    },
    "outcome": "completed",
    "reason": "rounds_exhausted"
  },
  "code": null
}
```

The default (non-quiet) output carries the same `terminal`, `finished` and `code` plus `quiescent`, `suspended`, `owner`, `result` and `status` — the `campaign_suspended` payload of a suspended run, `null` here, and the `campaign_result` and `campaign_status` projections `--quiet` omits. `--quiet` is a presentation control and changes nothing about the run itself. In either mode stderr ends with two lines, the continuation, `evo tree evolve: continue: set linked_from to <absolute path of ./demo/run-2> and seed_root to <absolute path of ./demo/run-2>/artifacts/champions/v1 (the target must allow trees under it)`, and the resilience summary, `evo tree evolve: Skipped rounds: 0 · Continuation turns: 0, led to an edit: 0`.

`./demo/run-2` above is a second campaign in its own fresh run directory; the same directory resumes the campaign already there rather than starting another. The run directory must lie under the target's allowed root, the manifest's directory by default, which both `./demo/run-1` and `./demo/run-2` do.

A finished sample campaign continues as a linked run. Copy `./demo/manifest.json` to `./demo/manifest-linked.json` in the same directory, so every relative path still names the scaffold's programs and corpus, and add the two keys the `continue:` line names — `"linked_from": {"run_dir": "run-1", "baseline": "fresh"}` and `"seed_root": "run-1/artifacts/champions/v1"`, both resolved against the manifest's directory — then, with the target still served from `./demo/manifest.json`, run `evo tree evolve --manifest ./demo/manifest-linked.json --run-dir ./demo/run-3`. The successor's seed is the predecessor's final champion copy, which lies under `./demo/run-1` and therefore inside the allowed root the target is already serving; a target served with another allowed root needs the tree copied under that root. The successor reports `linked_from` with `family: 14` and `first_round: 3` (the sample ran rounds 1 and 2), runs rounds 3 and 4 against its own baseline, and finishes `completed`; both its rounds close `clean`, as round 2 did, because its seed is the already-improved Champion. If the manifest changes a comparable field, or `seed_root` names a tree that is not the predecessor's final champion, the successor still runs: `evo tree evolve` prints `note: condition changed across the link` with one `differs:` line per difference before it starts, and the result reports the difference as a `condition_changed` caveat.

## Swapping the editor

The scaffolded `editor` section is `subprocess`. To point the same manifest at an OpenAI-compatible provider instead, replace it with a `chat` section:

```json
{
  "kind": "chat",
  "model": "<model id>",
  "base_url": "<endpoint>",
  "api_key_env": "<NAME>",
  "max_turns": 8,
  "retry": {
    "version": "role_call_retry/1",
    "max_attempts": 3,
    "total_budget_seconds": 90,
    "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 15, "jitter": "full"}
  },
  "context_compact_chars": 800000,
  "context_message_max_chars": 64000
}
```

`api_key_env` names an environment variable of the `evo tree evolve` process; its value is read at bind and never written into the manifest. Leave it out for an endpoint that needs no credential; the role then sends no `Authorization` header ([endpoints without credentials](manifest-v2.md#endpoints-without-credentials)). In both `/15` and `/16`, `editor.kind: chat` and `reflector.kind: chat` require the shared `role_call_retry/1` policy: it retries transient provider errors for one request at a time, retaining conversation and edits. A `chat` role also declares `context_compact_chars` and `context_message_max_chars`; 800 000 and 64 000 assume at least about 400 000 input tokens ([compaction keys](manifest-v2.md#compaction-keys-in-15)). See [Manifest v2](manifest-v2.md#editorkind-chat) for every key and [role resilience](manifest-v2.md#reflector-and-editor-resilience-family-10) for retry details.

The `reflector` section is swapped the same way and takes the same keys, minus `max_changed_files` and `max_written_bytes`, plus the ones it always carries: `{"kind": "chat", "model": "<model id>", "base_url": "<endpoint>", "api_key_env": "<NAME>", "max_turns": 8, "environment_docs": null, "probe": null, "max_consecutive_failures": 2, "retry": {...}, "context_compact_chars": 800000, "context_message_max_chars": 64000}`, beside the scaffold's `selection` and `families`. The two roles are independent — a `chat` Reflector beside the scaffolded `subprocess` editor is a valid document. See [Manifest v2](manifest-v2.md#reflector).

## What `serve` reads from the manifest

The artifact policy (`artifacts`), the loopback bind address (`target.base_url`) and the seed mode (`target.seed_mode`) come from the manifest, so the served target is the one the manifest describes. The target's capacities and semantic identity are its own constants and the manifest is validated against them before the socket opens: a `contract_digest` or `settings_hash` pin that differs from the reference values is refused with `pin_mismatch`, and a `min_input_bytes` above 1 MiB or a `max_response_bytes` below 1 MiB is refused with `limits_incompatible`. These are the same refusals the runtime's preflight would raise, moved to start time.

## The allowed root

The target loads trees only under one allowed root. By default it is the directory that holds the manifest; `--allowed-root DIR` overrides it. The root itself must exist and be a directory, otherwise `serve` refuses with `root_not_directory`. The manifest's `seed_root` must lie inside the root, otherwise `serve` refuses with `seed_outside_root`. The run directory a runtime creates must lie inside the root too, which the default layout satisfies. A linked run's `seed_root` is a champion copy inside the predecessor's run directory, so a successor served with the same allowed root as its predecessor can always load it.

## Ready line, refusals and exit codes

`serve` writes exactly one line to stdout, after the socket is bound and the serving thread has entered its loop:

```json
{"status": "listening", "base_url": "http://127.0.0.1:8765", "allowed_artifact_root": "/abs/demo", "seed_root": "/abs/demo/seed", "seed_mode": "exact", "policy_digest": "<sha256>", "boot_id": "<hex>"}
```

A clean stop on `SIGINT` or `SIGTERM` exits `0`. A refusal before the socket opens exits `2` with one line `evo-target: serve refused: <code>` on stderr and nothing on stdout. A socket that cannot be bound exits `1` with `evo-target: cannot bind <host>:<port>: <reason>`. A serving loop that stops unexpectedly exits `1` with `evo-target: serving stopped: <exception class>`. `init` exits `2` with `evo-target: init refused: <code>` for `not_a_directory`, `dir_not_empty` or `scaffold_failed`, and leaves the directory as it was.

To keep the ready line and diagnostics across service restarts, append them to a private file:

```bash
evo-target serve demo/manifest.json --log-file target.log
```

Expect the same ready line and a closing `exit 0` in `target.log` after a clean stop. The option opens before reading the manifest, creates an absent file as `0600`, and appends to an existing regular file only when it is owned by you, has one link and has no group or other permission bits. It never changes an existing file's mode. A symlink, FIFO or failed private-file proof prints `evo-target: serve refused: log_file_unsafe`; another opening or descriptor error prints `evo-target: serve refused: log_file_unavailable`. Both exit `2`. A later write failure disables logging with a note; a broken stdout or stderr stops receiving output and does not stop the logged service. Without the option, no process log or tee is installed. Keep the file outside every run directory. The copy is not redacted, because `serve` has no run secret set, so read it before sharing it. HTTP access logging stays off.

## Reference properties

The target admits four instances and refuses a fifth with `instances_exhausted`. It advertises eight concurrent sessions as a scheduling limit for conforming clients; the wire has no session-saturation code, so the limit is not enforced and a client that ignores it keeps its extra sessions until the instance is released. Input and output are bounded at 1 MiB each, and an answer is truncated after 4000 characters with `truncated` set. The semantic identity is `target_id` `reference`, `contract_digest` `reference-contract-1`, `settings_hash` `reference-settings-1`, `runtime_ref_digest` `reference-runtime-1`, with a fresh `boot_id` per start. The attested digests are computed over a private, read-only snapshot of the admitted tree that the library keeps under its own `snapshot_root`, never over the caller's live `candidate_root`, and that snapshot is removed at the instance's release or at the target's own close, whichever comes first.

## Embedding the target in tests

`evolution.tree` exports `ReferenceTarget` and `serve_reference_target` for a test suite that wants the target in its own process. `ReferenceTarget` is a `TargetServer` from the [target server library](target-server.md) bound to the demonstration backend; a target of your own is the same library with your backend.

```python
from threading import Thread

from evolution.tree import ReferenceTarget, load_manifest_v2, serve_reference_target

manifest = load_manifest_v2(path)
target = ReferenceTarget(allowed_artifact_root=root, policy=manifest.policy)
server = serve_reference_target(target)
Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
port = server.server_address[1]
...
server.shutdown()
server.server_close()
target.close()
```

`target.close()` releases every instance the target still holds — closing each open session and then each backend instance — and removes the snapshots it owns, so a suite that skips it leaks one snapshot directory per target it started.

`serve_reference_target` accepts only the three loopback hosts — `127.0.0.1`, `localhost` and `::1` — and raises `ValueError` for any other host before binding a socket.

The packaged target cannot be made to violate the wire. The fault-injection hooks the conformance suite uses to prove that the runtime rejects a non-conforming target live in the repository's test suite, not in the package.

## Relation to the `evo` command tree

`evo-target` is a separate console script, outside the `evo tree` commands (`evolve`, `conformance`, `authorize`, `evaluate`, `trace` and `watch`). Use `evo-target init` to create the demonstration files and `evo-target serve` to start their Target Service. See the [CLI reference](cli.md#tree-service-commands) and [Target server library](target-server.md) for the supported interface.
