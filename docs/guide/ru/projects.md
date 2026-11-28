# Проекты и сценарии

[English](../projects.md) · Русский

**Цель:** зарегистрировать агента как проект Lab и провести его через два
встроенных сценария — `connect` (зафиксировать, проверить запуск, измерить
baseline) и `evolve` (дать Core попробовать улучшить агента) — на бесплатном
демо-агенте без модели, сети и git. **Работа завершена**, когда демо-проект
прошёл `connect` и один цикл `evolve`, а оба результата видны в `lab project status`.

> **Впервые измеряете своего агента?** Начните с офлайн-команд, доступных с
> 0.0.3.dev72: `lab new-project DIR` создаёт шаблон, а `lab evaluate DIR`
> измеряет его без сервиса и реестра. См. [первое измерение](../first-evaluation.md#measure-your-own-agent).
> `lab project` пока не читает шаблон; эта страница посвящена реестру проектов
> и его сценариям.

Страница состоит из двух частей. **[Шаги](#шаги)** — список действий.
**[Справочные сведения](#справочные-сведения)** ниже объясняют Lab home, сокет,
детерминированную ошибку демо и то, чего Lab никогда не делает с рабочей копией.

Используйте новый каталог состояния, например `~/lab-state/dev81-b22`. Схема Owner 14 хранит подтверждения действий. Читайте, продолжайте и очищайте старые операции с породившими их wheel.

Новые факты операций dev60 привязаны к упорядоченному объявлению окружения и
байтам объявленных wheel и runtime-файлов, а также к исходным входам. Изменение
этих входов делает факты check/conformance/baseline устаревшими. Ранние
привязки сохраняют записанные источники и смысл digest; Lab не расширяет
старые доказательства на новые входы.

## Перед началом

- Пара установлена ([Установка](install.md)): `evolution-lab 0.0.3.dev81` с
  `evo-skills 2.0.0b22`, `lab` доступен через `PATH`.
- Запустите сервис с каталогом состояния и двумя wheel исполнения. Не указывайте
  `--socket`: без него сервис слушает `<home>/service.sock`
  ([Справочные сведения](#справочные-сведения)), поэтому и всем командам клиента
  ниже не понадобится `--socket`.

  ```bash
  mkdir -m 700 -p ~/lab-state/dev81-b22
  lab serve --state ~/lab-state/dev81-b22 \
    --execution-wheel "$(realpath ../wheels)/evolution_lab-0.0.3.dev81-py3-none-any.whl" \
    --execution-wheel "$(realpath ../wheels)/evo_skills-2.0.0b22-py3-none-any.whl"
  ```

  Новой паре (новой версии Lab или Core) нужен новый пустой каталог состояния:
  не используйте состояние предыдущей пары (ADR-0017; Lab не мигрирует состояние).
- Во втором терминале активируйте то же окружение
  (`source .lab/venv/bin/activate`). Если при `lab serve` вы задали `--home`
  (или `LAB_HOME`), передавайте то же значение каждой команде `lab` ниже.
- Вы знакомы с Bundle, Target Service и операциями ([Как работает Lab](concepts.md));
  проект не меняет эти понятия, а только записывает их принадлежность агенту.

## Шаги

1. [Создайте демо](#шаг-1-создайте-демо)
2. [Прочитайте статус](#шаг-2-прочитайте-статус)
3. [Подтвердите спецификацию](#шаг-3-подтвердите-спецификацию)
4. [Соберите пакет и выполните check и conformance](#шаг-4-соберите-пакет-и-выполните-check-и-conformance)
5. [Измерьте baseline](#шаг-5-измерьте-baseline)
6. [Выполните один цикл evolve](#шаг-6-выполните-один-цикл-evolve)
7. [Свой проект](#шаг-7-свой-проект)
8. [Работа с ассистентом](#шаг-8-работа-с-ассистентом)

### Шаг 1. Создайте демо

**Сделайте:** запросите у сервиса бесплатный демо-проект. Нужно настроенное
окружение сервиса (флаги `--execution-wheel` выше); без него вызов ответит
`409 service_environment_unconfigured`.

```bash
lab --json project demo demo
```

Отображаемое имя проекта — `Demo (demo)` (`Demo (<id>)`), поэтому несколько
демо различимы в списках. Это записывает рабочую копию в `<home>/clones/demo/`
без git и модели: `SPEC.md` и `spec.answers.json`, заготовку пакета оценки в
`lab/` и синтетического агента в `agent/`, который повторяет первую строку
своего файла навыка.

**Готово, когда:** `lab project show demo` сообщает `"state": "ready"`.

```bash
lab --json project show demo
```

### Шаг 2. Прочитайте статус

**Сделайте:** запросите, что сценариям `connect`, `evolve` и `restructure`
сейчас нужно от проекта.

```bash
lab --json project status demo
```

**Готово, когда:** вы видите четыре сценария: `connect`, `create-skill`
(создание одного навыка в реализации, начинается с `task`), `evolve` и
`restructure` (последний недоступен, пока реализация не является портом,
`lab project port`). У каждого есть список `steps` (для `evolve` —
`next_cycle.steps`): некоторые `ready`, остальные `blocked` до выполнения
предыдущих условий.

Каждый артефакт выполненного шага (`sources[]`, например `{"artifact": "SPEC.md", "digest": "sha256:…"}`)
содержит digest байтов, которые просмотрел Lab. Чтобы подтвердить именно этот
файл, передайте digest в `lab project confirm … --digest sha256:…`: если файл
тем временем заменили, подтверждение отклонится (`digest_mismatch`). Если шаг
заблокирован с `artifact_invalid`, каждая запись `invalid` указывает файл
(`artifact`), JSON Pointer внутри него (`path`) и сообщение.

### Шаг 3. Подтвердите спецификацию

**Сделайте:** первый человеческий шаг сценария `connect` — прочитать `SPEC.md`
и подтвердить это. Копия демо находится в `<home>/clones/demo/lab/SPEC.md`;
рядом поставляется `spec.answers.json`, файл ответов, который шаг `spec`
проверяет по схеме. Дождитесь, пока `lab project status demo` покажет `spec`
как `done`, прежде чем подтверждать: подтверждение до появления файла ответов
сразу устареет.

```bash
lab --json project confirm demo --implementation demo \
  --claim spec.confirmed --path SPEC.md --author <you> \
  --link catalogue:requirements --link file:spec.answers.json
```

**Готово, когда:** `lab project facts demo` показывает `fact spec.confirmed holds=yes`.

### Шаг 4. Соберите пакет и выполните check и conformance

**Сделайте:** зафиксируйте проект в Bundle, подключите его к этому поколению
Owner, запустите из него target и выполните обе проверки готовности. У
`project target` нет `--wait`: команда сразу возвращает ссылку на задачу, и вы
опрашиваете `task status` до завершения. Сохраните `BUNDLE_DIGEST`: он нужен
на каждом следующем шаге; после каждого запуска target сохраняйте новую пару
`TARGET`/`GENERATION`.

```bash
lab --json project bundle demo
# use the response's bundle.digest as BUNDLE_DIGEST

lab --json connect BUNDLE_DIGEST
# use the response's id as CONNECTION

lab --json project target demo --bundle BUNDLE_DIGEST
# use the response's task.id and task.owner_generation
lab --json task status TASK_ID --generation GENERATION
# repeat until state is "succeeded"; then use ref.id as TARGET, owner_generation as GENERATION

lab --json check CONNECTION --target TARGET --generation GENERATION --wait
lab --json conformance CONNECTION --target TARGET --generation GENERATION --wait
```

Conformance всегда останавливает свой target после завершения, поэтому только
что использованный target нельзя повторно применить для baseline.

**Готово, когда:** и `check`, и `conformance` отвечают `"state": "succeeded"`,
а `lab project facts demo` показывает `check.passed` и `conformance.passed` с
`holds=yes`.

### Шаг 5. Измерьте baseline

**Сделайте:** запустите новый target на том же Bundle (conformance остановила
предыдущий), затем подготовьте и запустите фиксированную оценку. В созданном
демо `evaluation-request.json` указан `"bundle_digest": null` —
`project prepare` отклоняет `null` с `invalid_request`, поэтому сначала
заполните его (копия не меняет собственный файл рабочей копии):

```bash
lab --json project target demo --bundle BUNDLE_DIGEST
lab --json task status TASK_ID --generation GENERATION
# a new TARGET / GENERATION

jq '.bundle_digest = "BUNDLE_DIGEST"' \
  <home>/clones/demo/lab/evaluation-request.json > /tmp/evaluation.json

lab --json project prepare demo --request /tmp/evaluation.json
# use the response's prepared.digest as PREPARED_DIGEST

lab --json project start demo --prepared PREPARED_DIGEST --target TARGET
lab --json status OPERATION
```

**Готово, когда:** `status` показывает `"control_state": "finished"`, а
`lab project facts demo` — `baseline.measured holds=yes` и `connected holds=yes`:
сценарий `connect` имеет состояние `produced`.

### Шаг 6. Выполните один цикл evolve

**Сделайте:** создайте запрос кампании для того же Bundle, подготовьте его,
запустите новый target, выполните кампанию до конца и экспортируйте изменения:

```bash
cat > /tmp/campaign.json <<'JSON'
{"kind": "campaign", "bundle_digest": "BUNDLE_DIGEST",
 "stop_grace_seconds": null, "cleanup_wait_seconds": null}
JSON

lab --json project prepare demo --request /tmp/campaign.json
# use the response's prepared.digest as CAMPAIGN_DIGEST

lab --json project target demo --bundle BUNDLE_DIGEST
lab --json task status TASK_ID --generation GENERATION
# a new TARGET / GENERATION

lab --json project start demo --prepared CAMPAIGN_DIGEST --target TARGET
lab --json status OPERATION

lab --json export OPERATION --wait
```

Запрос кампании имеет закрытую форму: все четыре ключа обязательны, а `null`
для двух тайм-аутов означает значение Lab по умолчанию. Храните файл запроса
**вне рабочей копии** (здесь `/tmp`; для ассистента — каталог проекта рядом с
`AGENTS.md`): файл внутри рабочей копии меняет входы реализации и делает
check, conformance и baseline устаревшими. Если ключ отсутствует,
`invalid_request` называет его в сообщении (`… missing: cleanup_wait_seconds`);
лишние ключи только подсчитываются.

**Готово, когда:** `status` показывает `"control_state": "finished"` и
`result.core.suspended` равно `false`, `export` завершается с `"state": "succeeded"`,
а `lab project status demo` показывает все шаги первого цикла `evolve` как `done`.

### Шаг 7. Свой проект

**Сделайте:** зарегистрируйте рабочую копию своего агента вместо демо:

```bash
lab --json project create acq --name Acquiring --path /home/you/acquiring \
  --root lab --impl agent=lab/agent,package=bundle-request.json
```

Запись пакета разрешается относительно корня материалов (`--root`). Когда
`package.present` равно false, `source.reason` и `source.entry` указывают
причину и место поиска Lab.

Для шага `spec` своего агента создайте два файла в корне материалов (`--root`):
`SPEC.md` и `spec.answers.json`. Выведите схему файла ответов командой
`lab scenarios schema connect spec-answers/1` и следуйте разделу `#spec`
скилла `connect` (`evolution-lab-scenario-connect`). Выполняйте команду
`lab project confirm` из шага 3 только после того, как `lab project status`
покажет `spec` как `done`: подтверждение до появления файла ответов ссылается
на отсутствующий файл и сразу устаревает.

Затем выполните шаги 3–6 для id и реализации своего проекта. Точные поля —
реализации, запись пакета, ссылки, мутации, факты, все маршруты и коды ошибок —
описаны в [API проектов](../../../lab/docs/api/projects.md).

**Готово, когда:** `lab project show <id>` сообщает `"state": "ready"`, а
`lab project status <id>` перечисляет те же четыре сценария, что и для демо
(`restructure` недоступен для нового проекта).

### Шаг 7a. Перенесите реализацию на шаблон

**Когда:** вы переносите агента на шаблон, например каркас Sylph. Lab не знает
шаблоны по именам: передайте git URL (или абсолютный путь локального
репозитория) и после `@` необязательную ветку, тег или коммит.

**Сделайте:**

```bash
lab project port acme --from agent --template https://git.example.com/team/sylph.git@v1
lab project show acme
lab project facts acme --implementation agent-port      # implementation.ported holds
lab project open acme                                    # impl/agent-port next to work/ and materials/
```

**Результат:** новая реализация `agent-port` (другое имя задаёт `--id`),
зарегистрированная как порт `agent`. Её место — отдельный git-репозиторий в
`~/.local/share/evolution-lab/ports/acme/agent-port` (или абсолютный каталог
`--into` вне рабочей копии и Lab home). Он содержит отслеживаемые файлы шаблона
в одном коммите, один коммит от вас (или от Lab, если git identity не задана)
и тег `lab/port/created`. Remote отсутствует: доставка порта и перенос кода
агента — ваша задача. `lab project show` перечисляет шаблон, ref и коммит в
`port` реализации. Учётные данные в URL шаблона никогда не сохраняются и не
показываются. Снимок создаётся через `git archive`: действуют
`export-ignore`/`export-subst` из `.gitattributes` шаблона, файлы Git LFS
поступают как указатели. Стандартный id `<source>-port[-N]` должен соответствовать
грамматике id длиной до 40 символов; для длинного id источника передайте `--id`.
Для переноса поведения агента пройдите сценарий `restructure`
(`lab project status acme --implementation agent-port --scenario restructure`,
скилл `evolution-lab-scenario-restructure`); затем выполните `connect` порта,
чтобы получить оценку качества. Затем задайте пакет порта через
`lab project impl set-package` и пройдите сценарий `connect` с
`--implementation agent-port`. Точные проверки, места назначения и ошибки —
в [API проектов](../../../lab/docs/api/projects.md#ports-lab-project-port).

### Шаг 7b. Агент изменился в репозитории команды

**Когда:** через несколько недель вы возвращаетесь к проекту, созданному с
`--clone`, а команда отправила новые коммиты (навык, инструмент, правки промпта).
Рабочая копия в `clones/<id>/` остаётся на коммите клонирования до вашего
запроса; фонового fetch нет.

**Сделайте:**

```bash
lab --json project sync acq                       # fetch and report; the working copy does not change
lab project sync acq                              # the same, as a short readable report
lab --json project sync acq --apply <sha>         # merge exactly the commit the report named
lab --json project status acq                     # what went stale because the files changed
```

Сначала прочитайте отчёт: `mode` (`fast_forward`, если у `lab/acq` нет
собственных коммитов, и `merge`, если есть, например пакет подключения),
коммиты, файлы и `dirty`. Apply сливает ровно прочитанный `upstream.sha`, ничего
более нового. При конфликте или наличии локального неотслеживаемого либо
игнорируемого файла, который команда теперь отслеживает, операция отклоняется с
`merge_conflict`, и копия остаётся прежней; `working_copy_dirty` означает, что
отслеживаемый файл изменён вручную (сначала закоммитьте или откатите его).
Факты и подтверждения не редактируются: изменённые файлы делают их устаревшими,
а `project status` перечисляет, что нужно перепроверить. `--rev R` проверяет
другую ветку, тег или коммит вместо стандартной ветки источника. Синхронизируется
только клон Lab; каталог, зарегистрированный через `--path`, вы обновляете git самостоятельно.

**Готово, когда:** `sync --apply` возвращает `lab-project-sync-result/1`, а
`project show acq` содержит `working_copy.source.base_revision`, равный
применённому sha. Подробнее: [Синхронизация](../../../lab/docs/api/projects.md#sync-lab-project-sync).

### Шаг 8. Работа с ассистентом

**Сделайте:** создайте рабочее пространство ассистента проекта. Оно полностью
перестраивается при каждом вызове — не редактируйте его вручную, редактируйте
рабочую копию.

```bash
lab --json project open demo
```

Команда записывает `<home>/workspaces/demo/`: `AGENTS.md` (и `CLAUDE.md` =
`@AGENTS.md`), `.claude/skills/` и `.agents/skills/` с десятью справочными навыками и
четырьмя навыками сценариев (`connect`, `create-skill`, `evolve` и `restructure`),
символьные ссылки `work` и `materials` на рабочую копию и
корень материалов проекта и `.lab-workspace.json` с парой, сокетом и двумя digest каталогов. Если extra `mcp` установлен там, где работает сервис, там же
лежат файлы клиентов сервера Lab MCP: `.mcp.json` (Claude Code),
`.codex/config.toml`, `.qwen/settings.json` и `.gigacode/settings.json`. Об
одобрении, доверии Codex и перезапуске ассистента — в разделе
[В рабочем пространстве проекта Lab](mcp.md#в-рабочем-пространстве-проекта-lab).

В рабочее пространство также входят руководства по Owner и сервису. Относительные ссылки в навыках открывают соответствующие файлы рабочего пространства.

Сгенерированный `AGENTS.md` задаёт правила ассистенту: для шага сценария он
сначала загружает скилл `evolution-lab-scenario-<id>`, затем читает
состояние проекта (инструмент `project_status` или команда CLI). Шаг завершён, когда это показывает
состояние проекта. Храните файлы запросов вне рабочей копии.

`lab project open demo --autonomy ask` — режим по умолчанию. Шаг `human_only` в этом режиме
требует подтверждения человека. Ассистент использует инструмент MCP или спрашивает в разговоре
перед выполнением команды CLI. В режиме `--autonomy yolo` ассистент выполняет запрошенное действие
без вопроса Lab. После смены режима перезапустите ассистента или переподключите сервер.

Для ассистента, который сам не читает `AGENTS.md`, выведите его вместо пути:

```bash
lab project open demo --print
```

**Готово, когда:** `<home>/workspaces/demo/AGENTS.md` существует, а его раздел
«Start here» содержит работающую команду `lab project status demo`. Команда
абсолютная (интерпретатор сервиса с
`-E -P -m evolution_lab.cli`) и несёт `--socket <path>`. Если extra установлен
и вы одобрили сервер в ассистенте, отвечает и его инструмент `project_status`
с `project` = `demo`.

## Справочные сведения

### Сокет находится в Lab home

`lab serve` без `--socket` слушает `<home>/service.sock`. Home — это `--home`,
иначе `$LAB_HOME`, иначе `$XDG_DATA_HOME/evolution-lab`, иначе
`~/.local/share/evolution-lab`. Каждая команда клиента определяет сокет так же:
сначала `--socket`, затем глобальный `--home`, `$LAB_HOME`, значение XDG и
резервное значение. Пока вы не передаёте `--socket` и всегда передаёте тот же
`--home` (или не передаёте вовсе) и `lab serve`, и клиенту, искать путь к сокету не нужно.

Если путь, полученный из home, не помещается в адрес Unix-сокета (около 107
байт), Lab отказывает вместо молчаливого выбора другого пути: CLI выдаёт
ошибку использования, `lab serve` — `socket_path_too_long` (этап `transport`).
Передайте более короткий путь в `--socket` — обычно это происходит, когда
`--home` (или `$XDG_DATA_HOME`) указывает глубоко вложенный каталог.

`lab guides` и `lab scenarios` никогда не обращаются к сервису: они отклоняют и
`--socket`, и `--home`.

### Детерминированная приостановка демо

При создании демо фиксирует loopback-порт, digest окружения сервиса и путь его
интерпретатора; после изменения окружения сервиса (пересборка wheel, перемещение
venv) или занятия порта создайте новое демо.

Пока существует `<home>/clones/demo.fault`, каждый ход демо-агента завершается
ошибкой самого target. Кампания на нём приостанавливается вместо завершения
ошибкой, с `result.core.suspensions[-1].reason == "target_unstable"` в
`lab status`. Это позволяет недорого отрепетировать `resume`, не дожидаясь
настоящего инфраструктурного сбоя:

```bash
touch <home>/clones/demo.fault
# start a campaign; watch it suspend

lab --json target stop TARGET --generation GENERATION --wait
rm <home>/clones/demo.fault
lab --json project target demo --bundle BUNDLE_DIGEST   # a fresh, healthy target
lab --json task status TASK_ID --generation GENERATION
lab --json resume OPERATION --target TARGET --generation GENERATION
```

Флаг находится рядом с рабочей копией, вне любого корня Bundle, поэтому его
создание или удаление никогда не меняет digest Bundle и не делает факты устаревшими.

### Чего Lab не делает

Lab никогда не пишет в репозиторий команды и не выполняет push. Он пишет в
рабочую копию только при её клонировании (`--clone`) и при выполнении
`lab project sync`, который выполняет fetch и merge в этот клон (`clones/<id>/`)
и никогда не затрагивает каталог, зарегистрированный через `--path`.
`project open` и `project demo` создают файлы только внутри Lab home
(`workspaces/`, `clones/`); подтверждение спецификации, принятие устаревшего
факта или запуск кампании никогда не затрагивают файлы по указанному `--path`
(или `--clone`) (Lab меняет клон только при создании и через `lab project sync`;
ассистенты и люди коммитят в `lab/<id>`), кроме содержимого экспортного ZIP
кампании — и этот ZIP никогда не применяется автоматически.

`lab project port` пишет только внутри Lab home (`ports/`, `tmp/`) или в
указанный вами каталог `--into` вне рабочей копии; Lab никогда не удаляет
каталог порта, даже при архивировании реализации.

**Далее:** [Результаты](results.md) подробно объясняют статус, отчёт, diff и
экспорт кампании, а [API проектов](../../../lab/docs/api/projects.md) — точные формы
проектов, ссылок, фактов и сценариев.

## Справочные материалы

- [API проектов](../../../lab/docs/api/projects.md) — Lab home, реестр, ссылки, факты,
  подтверждения, acceptance, каталог сценариев, все маршруты `/v1/projects` и коды ошибок.
- [Справочник локального сервиса и CLI](../../../lab/docs/api/service-s5.md) — запуск сервиса,
  глобальные флаги, ключи мутаций.
- [CLI и локальный сервис](cli.md) — те же действия для одного Bundle без проекта.

### Готовность первого измерения после изменений кампании

В dev66 `lab-project-facts/5` вычисляет `baseline.measured` из основы измерения
исходного Bundle оценки или точной цепочки acceptance. Изменения только
кампании могут сохранить этот факт; изменение материалов всё равно требует
новых check/conformance для `connected`. Используйте общие значения `holds`
для готовности; последняя терминальная оценка с ошибкой по-прежнему затеняет
более ранний успех. Для выбора по расписанию/метрикам используйте
`lab bundle measurements DIGEST`: среди Кандидатов есть незавершённые
результаты. См. [границы basis и acceptance](../../../lab/docs/api/projects.md#facts-lab-project-facts5).
Это относится к одной producing pair/состоянию; старые прогоны остаются со своими wheel.
