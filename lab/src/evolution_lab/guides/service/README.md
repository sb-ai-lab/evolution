# Local service guide

Use the optional `lab` service when a local client needs to call an already
prepared Lab Bundle, or prepare a new one, through a Unix socket. It is a foreground, one-UID service;
it is not a daemon, remote endpoint, shared worker, or replacement for direct
Python `PreparationStore` and `Owner` use.

Install the `service` extra with the exact Lab distribution that will produce the
new state; the direct Python APIs and ordinary CLI help do not import its server
dependencies. The installed console is `lab = evolution_lab.cli:main`.

Choose a fresh private state directory and socket parent. Preserve the exact
Lab/core wheel archives and declared runtime files for the new state. Do not
copy an old state, venv, receipt, archive, or run into it: there is no migration
or folder copying for Owner state. The latest released pair is Lab `0.0.3.dev80` / exact core `2.0.0b21`.
This guide describes development source, including schemas newer than that release. See `docs/RELEASES.md` for delivery
history and source provenance. Choose a new
private state path for the producing pair; this guide creates no directory. Owner
SQLite schema13 upgrades a schema11 or schema12 state in place at connect, after which an older Lab
refuses it, and refuses earlier Owner schemas with `unsupported_schema`.
A matching schema does not make another pair's state reusable. Old runs, resume and
cleanup stay with their producing wheels and state. Campaign reads cover family 14 and 15
through `lab-campaign-view/15`; the Owner report is `lab-owner-report/15`; status/list use `/8`.

For family 15, view `/15` also carries `checks`, `checks_state` and
`checks_reason`; the nested Owner report `/15` carries `lab_checks`,
`lab_checks_state` and `lab_checks_reason`. An admitted projection has state
`present` and null reason: checks is null if Core has neither map, otherwise
`{modes: {...}, shadow_hits: {...}}` with an absent map copied as `{}`.
An unreadable projection has null checks, state `unavailable`, reason `unreadable`.
These fields remain independent of binding-read failures and `independent`.
Family 14 has none of these keys. Modes and individual counts are copied from
Core; counts of different checks are not comparable and must not be summed.

Preparation accepts manifests `/15` and `/16`; `linked_from` is refused as `linked_run_unsupported`. Bundle/prepare records only declared inputs. They do not start a
target, scorer, check, conformance, campaign, or evaluation.

Owner SQLite schema13 retains reflector-probe scope records, per-start probe
facts and the separate per-start Core-terminal columns introduced in v8, and
adds each start's Core refusal findings (schema12) and prepared digest (schema13).
Connect upgrades a schema11 or schema12 state in place, after which an older Lab
refuses it; earlier Owner schemas are refused with `unsupported_schema`, without
migration. Deploy with a fresh private state for the producing pair. A matching
schema does not make another producing pair's state reusable. These instructions
name a prerequisite; they do not create state or upgrade old runs.

The exact Core archive SHA-256 is
`578f26a0c9c27f63a56fb049f90e4eb51c96e7180c429e333b25255247e0f6aa`.
Record the exact Lab wheel hash too; bundled guide edits change its bytes.

## Lifecycle and saved references

Every action is explicit: serve; Bundle; prepare; bind; target start; evaluation
start; status/events/report; target stop; then deletion of released results.
Connections disappear on service restart and must be rebound. Save every
TaskRef/TargetRef with its producing `owner_generation`; never guess a generation
from health. Mutations are never retried automatically. A CLI exit `125` means
acceptance is unknown, while `124` or `130` after a printed task ref interrupts
only local waiting.

## Start, read and stop

Run the service with `lab serve --socket ABS_PATH --state ABS_DIR`. Put global
`--socket`, `--json`, and `--timeout` before normal commands. `--json` is the
complete public result. A stale socket is removed only manually, after the
operator has established that its former service has stopped. For an orderly
shutdown signal the service PID alone. Terminal Ctrl-C affects the foreground
group and has weaker target/helper cleanup guarantees.

The service never launches check or conformance on its own, and sampled SSE is
only current state/progress with no replay. Process exit, HTTP success, coverage,
and report availability are evidence with limits, not quality, cost, or cleanup
claims. Malformed local request, secret or policy contents currently exit `1`
rather than usage `2`. Use `--json` when you need all response fields.

Use this explicit command sequence, after preparing private request files: `serve`
→ `bundle create` → `prepare` → `connect` → `target start --wait` → `start` →
`status`/`events`/`report` → `target stop --wait` → `delete-results --wait`.
With `--json --wait`, stdout is two JSON lines (TaskRef, then the final TaskStatus); use the last line
(on exit 124, a local wait timeout, only the task reference was printed).
For every normal command, place `--socket ABS_PATH --json --timeout SECONDS`
before the command. Campaign-only `diff`, `export --wait`, and `export-content`
are separate explicit actions; `no_change` has no ZIP. The package source
distribution contains the full route table and request templates at
`docs/api/service-s5.md`. The Python-only preparation guide remains the source
for creating a Bundle without running it.

A suspended campaign operation (a finished or released one, on a ready target
of the same Bundle) continues explicitly through `POST
/v1/operations/{id}/resume` — CLI `lab resume OPERATION --target TARGET
--generation GENERATION [secrets as for start]` — the same mutation class and
key rule as `start`. `status`'s `continuation:` line names the exact command,
including `lab cleanup operation OPERATION` first when the operation is
`cleanup_required`/`orphaned`. It follows the status field `resume`
(`lab-operation-status/8`): `ready`, `cleanup_first`, `busy` or `refused` with
the refusal code `resume` would return, or `null` when there is nothing to
continue; `ready` does not cover the call-time target, secrets and slot
checks. After a `target_unstable` suspension the verdict's `target` key is a
requirement, `{requirement: fresh_instance, excluded: [...]}`: continue on a
target the campaign has not used (`target_unstable_fresh_required`, 409, before
any start is recorded), and the `continuation:` line says so; when a ready target of the same
Bundle that the campaign has not used exists, the text-mode line names it (`lab agent list
--ready` lists candidates). See `docs/api/service-s5.md`.

If the core refuses the campaign, the status carries `failure.code:
core_refused` and `refusal {code, path, class, instance_digest}` (`class` is
`hard` or `authorizable`) — at the top level and in the `starts[]` entry of the
refused start; a start after an accepted `resume` begins with `refusal: null`.
After it comes the usual cleanup (`cleanup_required`, then `lab cleanup
operation`), which keeps `failure` and `refusal`. The class does not say
whether the campaign can continue; the `resume` field does. `lab status`
prints `refused: <code>[ at <path>] (<class>)`.

Read-only views of a run (all `GET` routes, each with a command): `status` carries `progress` and
`times` (each `null` beside a `progress_state`/`times_state` of `not_started` or `unavailable`);
`lab list --summary` gives one light row per operation; `lab campaign OPERATION` shows rounds,
attempts grouped by outcome (`accepted`, `agent_worse`, `not_enough_data`, `infrastructure`,
`no_candidate`, `stopped`) and champions, plus each round's baseline version and Reflector hypotheses and
where a failed attempt stopped (codes only); `lab measurements OPERATION [MEASUREMENT]` shows
coverage, means, the difference to the baseline and target-call tokens (not money, and not editor
or scorer tokens); `lab diff OPERATION [--from REF --to REF]` compares any two saved trees. When
`lab serve` is ready it prints `lab serve: listening on <socket>` on stdout. The service still
serves no pages: it stays a local API.

The panel shows every recorded final entry in core order, settings, roles,
connection and baseline refreshes, plus confidence `margin` and estimate `exact`.
Final entries include members, contrasts, selection, `listed` and next baseline.
Finalist, final decision and Champion transition are separate facts: only present
`champions[]` lineage with `origin: promoted_attempt` and the matching attempt ID
confirms promotion. A recorded transition does not assert the current Champion.

`Owner.suspensions`, GET `/v1/operations/{id}/suspensions` and
`lab suspensions OPERATION [--json]` share the closed `lab-suspension-evidence/1`
read. Its identifiers and codes explain recorded pauses, not all remaining work;
counts, digests and truncation remain explicit. The panel reads current evidence
on the suspended screen and loads history after resume when opened. New sequence
values invalidate old answers; failures mark retained evidence stale, and result
deletion clears it. Unavailable or stale history neither proves there were no
pauses nor confirms a current cause.

## Строгость записи и временный корень

По умолчанию Core и Lab используют `EVOLUTION_DURABILITY=relaxed`; незаданное
или пустое значение также означает `relaxed`. Все базы Lab, включая бюджетный
журнал шлюза, используют WAL с `synchronous=NORMAL`. Рядом с базой могут
оставаться файлы `-wal` и `-shm`: сохраняйте их вместе с базой. Python-код Core
и Lab не вызывает fsync. SQLite сохраняет свои синхронизации контрольных точек.

`EVOLUTION_DURABILITY=strict` возвращает прежние fsync и настройки SQLite:
реестр проектов — WAL с `synchronous=FULL`, базы Owner, проб и бюджетный журнал —
DELETE с `synchronous=FULL`. Другие значения, включая `Strict`, отклоняются
до записи. Задавайте переменную до запуска процесса и не меняйте во время работы.

`lab serve --durability {strict,relaxed}` переопределяет переменную для всего
сервиса, его файловых записей, баз и запускаемых процессов. Без флага сервис
следует переменной окружения. Lab передаёт действующий режим при запуске Core,
служебных процессов и встроенного Target launcher `/2`–`/3`, заменяя сохранённое
значение в копии окружения; Bundle и prepared-данные не меняются. Встроенный
launcher также наследует `TMPDIR` Lab, если в окружении цели нет своего значения.
Эти настройки видны агенту в `LaunchContext.env`.

В `relaxed` при потере питания или падении ядра ОС могут потеряться последние
закоммиченные транзакции, в том числе списания бюджета: исключения для бюджетного
журнала нет. При падении только процесса Lab закоммиченные записи сохраняются.
После сбоя хоста Owner восстанавливается по сохранившимся записям и может
отказать в продолжении, если обязательных подтверждений не осталось. Выбранные
режим и временный корень сервис пишет в лог при старте; видимость зависит
от настройки логирования.

`--scratch DIR` по умолчанию не задан. Без него пути остаются прежними, а каталоги
целей и снимков на state root сохраняются после остановки. С флагом Lab создаёт
`DIR/<key>/`, где `<key>` — первые 16 шестнадцатеричных символов SHA-256 абсолютного
пути state root. `DIR` должен быть абсолютным, без символических ссылок в цепочке,
принадлежать текущему пользователю и иметь права `0700`; отсутствующий каталог
создаётся с этими правами. Он не может содержать state root или находиться внутри
него, а также внутри `clones` или `workspaces` дома Lab.

Во временный корень переносятся только черновики:

| Данные | С `--scratch` |
|---|---|
| Приватный staging вызова diff/экспорта | `DIR/<key>/staging/<call_id>` |
| Рабочий каталог цели | `DIR/<key>/targets/<id>` |
| Контейнер снимков цели | `DIR/<key>/snapshots/<id>` |

`artifacts/`, `scopes/`, базы, квитанции, отчёты, Bundle/PreparedExecution и
опубликованные результаты остаются на постоянных путях. ZIP экспорта по-прежнему
публикуется рядом с корнем прогона. Staging каждого вызова приватен; после
завершения ввода-вывода убирается только его собственный `<call_id>`.

После записи подтверждённой остановки цели (`stopped`, `cleanup.state=confirmed`,
`cleanup.complete=true`) Owner убирает её рабочий каталог и контейнер снимков
в scratch. Это относится и к явной очистке осиротевшей цели прошлого поколения.
Удаление идёт относительно открытых дескрипторов каталогов без следования
символическим ссылкам. Ошибка уборки не меняет сохранённое состояние цели и не
становится отказом: сервис записывает предупреждение в существующий JSON-поток
диагностики. При следующем открытии Owner повторяет уборку оставшихся каталогов
только для уже подтверждённо остановленных целей и удаляет остатки `staging/*`.
Цели без подтверждённой остановки, включая осиротевшие `cleanup_required`,
автоматически не очищаются и продолжают занимать место. Найдите их через
`lab agent list` и запросите явную очистку: `lab cleanup target TARGET_ID --wait`.
Ручное освобождение или отметка `not_needed` не заменяют подтверждённую остановку
для автоматического удаления каталогов.

Файл `<state>/scratch.json` со схемой `lab-owner-scratch/1` хранит привязку к
`DIR/<key>`; схема Owner — v13. При совпадении привязки сервис продолжает
работу; при первом включении флага записывает маркер. Смена `DIR` или отказ от
`--scratch` разрешены, только если в записанном корне больше нет записей в
`targets/` и `snapshots/`. Иначе открытие отклоняется с `invalid_argument`, а
диагностика старта указывает прежний корень. Запустите сервис с прежним флагом,
остановите или явно очистите цели; удалить каталоги вручную можно, только убедившись,
что процессов больше нет. Когда старый корень пуст, маркер обновляется или
удаляется при отказе от флага. Исчезнувший корень считается пустым. Повреждённый
или нечитаемый маркер, включая FIFO и символическую ссылку, вызывает отказ,
а не перепривязку.

Держите `DIR` на tmpfs с запасом места и inode либо на отдельной файловой системе.
На стенде dev63 каталог `targets` занимал 766 МБ и около 109 тысяч файлов — это
наблюдение, а не лимит или SLA. Снимки живут, пока жива цель; при длинной кампании
объём растёт, и Lab его не ограничивает. tmpfs исчезает при перезагрузке:
для временных каталогов это допустимо, но их содержимое не служит постоянным
хранилищем результатов.

Пример запуска с осознанным выбором менее строгой записи и tmpfs для черновиков:

```bash
lab serve --socket /tmp/lab.sock --state /absolute/private/pair-state \
  --durability relaxed --scratch /tmp/lab-scratch
```

## Use the service's environment

To let the service own the execution environment, start it with the exact wheel
archives (and any runtime files) of its own interpreter:

```bash
lab serve --socket ABS_PATH --state ABS_DIR \
  --execution-wheel ABS_LAB_WHEEL --execution-wheel ABS_CORE_WHEEL
```

The service captures that environment before it creates state or the socket; a
refused capture exits with `environment_invalid` or `environment_unavailable`.
`lab health` then reports `service_environment` with the interpreter path
(`executable`) but without wheel or runtime-file paths. Its `digest` is
the value for `environment_ref` in a launch file. A Bundle request file can name
the service environment instead of local paths, as the first environment only:

```json
{
  "source": {"kind": "campaign", "manifest_path": "ABS_MANIFEST", "launch_path": "ABS_LAUNCH"},
  "environments": [{"kind": "service"}],
  "env": {}, "secret_names": [],
  "roots": [{"alias": "project", "source_path": "ABS_SOURCE"}],
  "path_bindings": []
}
```

A service started without `--execution-wheel` refuses it with
`service_environment_unconfigured`. The Bundle stores the same descriptor as the
explicit form, so the digest does not depend on which form was used. If a wheel
changes after the service started, Bundle creation is `environment_invalid`
and nothing is published; restart the service with the intended archives.

## Repeat a command after a lost reply

A mutation is a request that changes state, such as starting a target.
Every CLI mutation carries a mutation key: the CLI reads the current generation
from health and generates a request id, unless `--request-id ID --generation G`
or `--no-request-id` precede the command. On exit `125` (`acceptance_unknown`)
the output includes `request_id` and `owner_generation`. Repeat the same command
with them to learn the outcome without a second action:

```bash
lab --socket ABS_PATH --json --request-id REQUEST_ID --generation GENERATION \
  bundle create --request ABS_REQUEST
```

The CLI never repeats on its own. Keys live only for one service generation;
after a restart the old key is refused with `request_unknown_generation`, so
inspect state and decide explicitly. The same key with different parameters is
`request_conflict`. The key table and error codes are described in
`docs/api/service-s5.md`.


Core storage failures such as `persistence_failed` appear as `core_terminal`
`{owner, terminal, code, hint}` in status/list `/8` and Owner report `/15`.
`hint` is bilingual `{ru, en}` when a repair hint is known, otherwise null.
Each `starts[]` entry keeps its own terminal; the top-level field copies the last
start. An accepted resume opens a start with null `core_terminal`. Successful
finishes, suspensions and refusals have null `core_terminal`; suspension and
refusal retain their existing fields. CLI status/watch print the terminal and
its hint. The closed Lab `failure` codes and cleanup rules are unchanged.

<a id="reflector-probe"></a>
## Reflector probe evidence and start refusals (dev62)

Campaign view `lab-campaign-view/15` (both families) and Owner report
`lab-owner-report/15` add the same **top-level** closed triplet, separate from
Core's nested report:

| Field | Closed meaning |
|---|---|
| `reflector_probe` | A record with exactly `launched_by`, `state`, `restarts`, `last_exit`, or null when unavailable |
| `reflector_probe_state` | `present` or `unavailable` |
| `reflector_probe_reason` | null when present; `unreadable` when unavailable |

`launched_by` is `lab` or `operator`; `state` is `running`, `stopped` or
`exhausted`; `restarts` is a nonnegative integer count of replacement attempts
in this worker run; `last_exit` is a signed integer exit code or null. Only an
explicit record `{launched_by: null, state: not_declared, restarts: 0,
last_exit: null}` means no declaration. Missing or malformed Owner evidence is
null/unavailable/unreadable, never inferred absence. This availability is
independent of Core results, checks, binding reads and independent acceptance.
JSON report retains readable Owner facts even if Core is unavailable; Markdown
keeps its preexisting `report_unavailable` contract without a Core report.

An operator probe is checked once at start (also on explicit resume), using
`reflector.probe.request_timeout_seconds`. Its `running`/`stopped` values record
participation in that worker run, not fresh external liveness; Lab never stops
that server and its `last_exit` is null. Raw launch/exit/restart/exhaustion notes
are in private worker `stderr.log`. CLI/watch consume sampled status and show
aggregate probe facts, not a raw-note event stream. The panel shows all four
fields beside checks with independent availability.

| Lab failure code | Fixed message |
|---|---|
| `reflector_probe_unavailable` | The declared reflector probe is unavailable. |
| `reflector_probe_port_busy` | The reflector probe port is already in use. |

These are Lab failures at `before_start`, separate from Core `refusal`;
status/list `/8` and event `/3` retain their forms. On exact b22, `/15` and
`/16`'s default or explicit `suspend` refuse unavailable health before worker
spend. Explicit `/16` `on_unavailable: continue` dispatches with the operator
note and lets Core own reflection availability. Runs declaring that key must
stay with their producing pair; b13 cannot read this declaration. For launch consistency, required `/3` fields, bounded restarts,
explicit-resume counts and helper cleanup, see the
[campaign probe launch guide](../campaign-package/SKILL.md#reflector-probe).

## Generic HTTP evaluation preparation

`lab-service-prepare-request/5` extends evaluation `/4` with nullable
`max_consecutive_unmeasured` (positive integer N or `"off"`; null resolves to 3
unless the evaluation input supplies another value). `/4` adds nullable
`sample_retries`, `min_coverage`, `concurrency`, `scorer_concurrency` to `/3`;
`/3` adds `split` to `/2`. Every key of the selected closed raw form is required.
`/2`, `/3` and `/4` remain accepted with their own key sets. Campaign requests
reject evaluation-only keys. The new-preparation omission defaults apply to all
accepted request majors; historical saved plans retain their semantics.

CLI request files omit `schema`. `lab prepare` selects `/5` when
`max_consecutive_unmeasured` is named, even null; only `/4` settings select `/4`,
only `split` selects `/3`, otherwise `/2`. It fills remaining nullable keys for
the selected form. Project wrappers retain `/1` and `/2`; use generic preparation
for new settings.

<a id="bundle-measurements"></a>
## Measurements for a Bundle

`GET /v1/bundles/{digest}` and `lab bundle show DIGEST` return
`lab-bundle-summary/3` (`/2` plus nullable `measurement_basis`). The basis is a
SHA-256 of the saved fixed-evaluation inputs, not of the entire campaign manifest.
A null basis is unknown/unavailable, never a match. Unknown Bundle digests have
the same refusal as Bundle show.

`GET /v1/bundles/{digest}/measurements`, `lab bundle measurements DIGEST` and
`await owner.bundle_measurements(digest)` return the closed
`lab-bundle-measurements/1` form. The CLI service client uses
`ServiceClient.request_json("GET", f"/v1/bundles/{digest}/measurements",
expected_schema="lab-bundle-measurements/1")`. The top-level fields are exactly
`schema`, `bundle_digest`, nullable `measurement_basis` and `items`. Only fixed
evaluations from this Owner state with the same basis are included; a null
requested basis gives an empty list. The read creates no Bundle, captures no
environment and does not start work.

Each `items[]` object has `operation_id`, `prepared_digest`, the original
`bundle_digest`, `same_bundle`, `control_state`, `results_state`, nullable
`failure`, `times`, nullable `outcome` and nested `plan`. `same_bundle: false`
means the original evaluation used another Bundle with the same basis. Unfinished
and incomplete evaluations remain visible: these are input-equivalent candidates,
not decisions that the measurement is eligible as a baseline.

| Nested object | Fields |
|---|---|
| `plan` | `evaluation_set_id`, `split`, `case_count`, `case_ids_digest`, `repeat_seeds`, `max_turns`, `timeout_seconds`, `metric_plan_digest`, `concurrency`, `scorer_concurrency`, `sample_retries`, `min_coverage`, `max_consecutive_unmeasured`, `call_retries`, `max_failed_turns` |
| `outcome` | `complete`, `coverage`, `stopped_reason`, `unmeasured`, `min_coverage` |

`split` is train/test for campaign inputs and null for evaluation-only inputs.
Case and metric digests use canonical saved plan data; ordered `case_ids` define
the case digest. Old plans can have null `sample_retries`; that describes the saved
plan, not the current retry default. Repeat seeds may contain null according to
the target's seed mode. `max_consecutive_unmeasured` shows effective 3 even when
canonical storage omits it; `call_retries` is the saved object or null when call retries are disabled.
`outcome` is null if no result is available; `outcome.complete` and `coverage` may
also be null. Status/failure fields retain the public operation meanings;
unavailable/unknown results do not prove success. `times` carries public event
times, which can be missing. Terminal candidates precede active candidates;
within each group newer `last_event_at` (terminal) or `first_event_at` (active)
comes first, missing times last, with deterministic operation-id ties. These
are event-time proxies, not a separate persisted finish timestamp.

Choose candidates by the saved plan conditions and outcome. Different failure
settings do not themselves prevent a basis match; they describe how the result
was obtained. For first-measurement readiness use shared `baseline.measured` and
`connected` facts (`lab-project-facts/5`), which apply Lab's latest-terminal-failure
policy as well as the freshness rules below.

A successful baseline remains current when its effective `inputs_digest` already matches the current
inputs, without requiring a readable original Bundle, a current Bundle or a source receipt. An exact,
unbroken non-revoked acceptance chain is also sufficient. Only when neither route holds can the original
evaluation Bundle's non-null measurement basis rescue the stale fact. That basis-reuse route requires
valid current materials and matching request metadata, descriptor and source receipt in this pair/state;
reading facts never creates a Bundle or captures an environment.

Compatibility limitation: request `env`, `secret_names`, paths or `path_bindings` outside `inputs_digest`
do not by themselves invalidate an unchanged-input fact. Descriptor/receipt proof protects basis reuse
after material changes, not that legacy unchanged-input route. A missing proof disables only basis reuse.
`connected` still requires all three evidence facts to hold, check and conformance to share an effective
inputs digest, and baseline to match that digest or the original basis of the checked Bundle.

Fresh checks and conformance remain required on changed materials. The basis uses
the original evaluation Bundle, not an acceptance's destination: scorer A →
acceptance B → campaign-only edit C still compares original basis A with C and can
need a new acceptance. Revocation breaks the acceptance chain; basis reuse still
requires matching pair/place and the current valid package. Scope of basis reuse
is this pair/state only. Old runs retain their producing wheels/state, and no quality
claim or campaign-baseline reuse follows from discovery.
