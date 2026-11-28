# Демо: что показывает `just demo`

[English](../demo.md) · Русский

**Цель:** за пару минут увидеть, что Lab делает с агентом, и узнать, какие
файлы открыть после запуска. Оба примера работают с синтетическим агентом
офлайн: без модели, сети и ключей API. Каждый занимает несколько секунд и
сохраняет результаты в `.lab/demo/`. `just demo` построен на том же шаблоне
проекта, который вы будете использовать для своего агента.

| Команда | Что показывает | Рабочая директория |
|---|---|---|
| `just demo` | Lab **измеряет** агента как есть (фиксированная оценка) | `.lab/demo/evaluation/` |
| `just demo-campaign` | Evolution Core **улучшает** агента, а Lab запускает и измеряет его (кампания) | `.lab/demo/campaign/` |
| `just demo-panel` / `just demo-panel campaign` | те же результаты в браузерной панели | — |

Сначала выполните `just setup` ([README](../../../QUICKSTART.ru.md)).
**Target Service** — агент, работающий как локальный сервис для Core; **Bundle** —
неизменяемая копия входов; **conformance** и **check** проверяют, что target
говорит на протоколе ходов. Подробнее — [Как работает Lab](concepts.md).

## `just demo`: измерение агента

`just demo` выполняет те же две команды, которые вы запустите для своего агента:

```sh
lab new-project .lab/demo/evaluation/project
lab evaluate .lab/demo/evaluation/project --wheel <Lab wheel> --wheel <core wheel>
```

Проект демо — именно тот **шаблон проекта**, который создаёт `lab new-project`.
Последовательность действий:

1. `lab new-project` записывает шаблон в `.lab/demo/evaluation/project/` и
   выводит дальнейшие шаги для собственного проекта.
2. `lab evaluate` копирует проект в директорию запуска и заполняет поля
   `"lab:auto"`: digest окружения, свободный порт, интерпретатор оценщика и
   digest идентичностей.
3. Lab фиксирует копию в Bundle, запускает агента как target и выполняет
   conformance: Core отправляет набор проверок протокола. Затем Lab останавливает target.
4. Lab запускает новый target и проводит **фиксированную оценку**: 3 вопроса ×
   1 повтор = 3 образца. Каждый ответ оценивается.
5. Lab останавливает target, проверяет, что ничего не осталось работать, и
   выводит результаты.

### Агент, адаптер и что нужно заменить

Шаблон хранит агента и адаптер в отдельных файлах:

| Файл | Назначение | Для своего агента |
|---|---|---|
| `agent/my_agent.py` | **Агент.** Заглушка без модели: `MyAgent(prompt).answer(question)` отвечает известным кодовым словом. Промпт требует вывести только это слово. Агент ничего не знает о Lab. | Замените: импортируйте здесь своего агента. |
| `agent/adapter.py` | **Адаптер.** Модуль, загружаемый Lab (`launch.json` → `agent`): `build(context)` и класс с пятью методами, вызывающими `MyAgent`. На проверки conformance он отвечает без вызова агента. | Сохраните структуру и измените места с `# TODO(you):`. |
| `seed/skills/SKILL.md` | Промпт агента; адаптер читает его из измеряемой копии. | Ваш промпт. |
| `corpus.jsonl` | Три вопроса `q1`–`q3`, ожидаемое кодовое слово — в `oracle.expected`. | Ваши вопросы и ожидаемые ответы. |
| `scorer/score.py` | Возвращает **1**, если ответ совпадает с ожидаемым текстом после `strip()` и case-folding, иначе **0**. | Правило правильности для вашего агента. |
| `launch.json`, `evaluation-input.json` | Как Lab запускает адаптер и что измеряет. | Меняйте только поля, отмеченные «yes» в `README.md` шаблона; оставьте `"lab:auto"`. |

Каждую правку объясняет английская страница [Connect your agent](../connect-agent.md).

### Вывод

```text
Samples: case, question -> the agent's answer [score]
  c-3a6eb0b2… (evaluation)
    q1  What is the code word for alpha? -> A1   [score 1]
    q2  What is the code word for beta? -> B2   [score 1]
    q3  What is the code word for gamma? -> unknown   [score 0]

Lab filled: environment digest, port, scorer Python, 3 identity digests (listed in resolved.json); nothing to do.
run 001: measurement accepted: 3/3 samples scored; mean score 0.67 (2 of 3 scored 1)
```

Перед образцами вывод описывает шаги, после них перечисляет файлы запуска и
указывает, какой открыть первым. **Ноль за `q3` ожидается:** заглушка знает
слова для `alpha` и `beta`, но не для `gamma`. Это показывает, что оценщик
действительно сравнивает ответы. Измерение принято, потому что оценены все
три образца. Фиксированная оценка записывает текущие результаты и не меняет
агента; кампания предлагает изменения и измеряет их.

В последней строке *accepted* означает, что оценен каждый образец и запуск
пригоден для использования; *mean score* показывает качество — здесь два
правильных ответа из трёх. Строка `Lab filled: ...` называет автоматически
заполненные поля, а `resolved.json` содержит их значения. Полный вывод, коды
завершения и файлы описаны в английском разделе
[Measure your own agent](../first-evaluation.md#measure-your-own-agent).

### Рабочая директория

```text
.lab/demo/evaluation/project/     the project skeleton: the same files you get from lab new-project
  agent/my_agent.py               the agent (a stub)
  agent/adapter.py                the adapter Lab loads
  seed/skills/SKILL.md            the prompt
  corpus.jsonl                    the three questions
  scorer/score.py                 scores one answer
  launch.json                     how Lab starts the adapter
  evaluation-input.json           what to measure: corpus, scorer, metric, repeats
  README.md                       what to edit, field by field
  .lab/
    state-<pair>/                 Lab's internal state: Bundle, runs, logs
    runs/001/
      conversations.md            every sample: question, answer, score, scorer verdicts
      report.md                   Lab's report of the evaluation
      resolved.json               the fields Lab filled in, the Python and the wheels
      summary.json                the facts the command checked
      evaluate.log                the steps of the run
      source/                     the project copy this run measured
    latest -> runs/001
```

Сначала откройте **`conversations.md`**: для каждого образца там вопрос Core,
ответ агента и результат оценщика. **`report.md`** — тот же отчёт, который
возвращает `lab report`: покрытие, средняя оценка и таблица образцов. Его
заголовки русские, как в приложении auto-evolve. Подробнее —
[отчёты об оценке](../results.md#evaluation-reports).

`state-<pair>/` — рабочая директория Lab. Её папки названы по digest и ID и не
предназначены для ручного чтения; используйте команды `lab` или панель.

## `just demo-campaign`: улучшение агента

Демо кампании использует тот же шаблон проекта через [`examples/owner/campaign.py`](../../../lab/examples/owner/campaign.py). Адаптер, корпус, оценщик и примеры ролей остаются прежними. Демо применяет профиль observe и заменяет `agent/my_agent.py` синтетическим агентом, который знает все три кодовых слова, но отвечает правильно только после появления `IMPROVED:` в первой строке skill. Кампания может менять `source/seed/skills/SKILL.md`.

1. Пример создаёт проект, манифест кампании `manifest.json`, **Редактор**
   `editor/edit.py`, предлагающий изменения `SKILL.md`, и **Рефлектор**
   `reflector/reflect.py`, объясняющий Редактору неудачные случаи.
2. Lab фиксирует проект в Bundle, запускает target и выполняет check.
3. **Раунд 1.** Core измеряет исходную версию seed `v0`: оценка 0. Редактор
   заменяет первую строку `SKILL.md` на `IMPROVED: reference skill`. Core
   измеряет Кандидата: оценка 1; Кандидат становится Чемпионом `v1`.
4. **Раунд 2.** Core измеряет `v1` как новый baseline. Улучшать больше нечего,
   раунд заканчивается со статусом `clean`, кампания завершается.
5. Lab читает изменения между `v0` и `v1` и экспортирует Чемпиона.

Образцы показывают изменение ответов:

```text
  m-r00-champion (baseline, round 1)
    q1  What is the code word for alpha? -> not improved   [score 0]
  m-r01-a01 (attempt, round 1, attempt r01-a01)
    q1  What is the code word for alpha? -> A1   [score 1]
  m-r01-a01-parent (other)
    q1  What is the code word for alpha? -> not improved   [score 0]
  m-r01-final-01-a01 (baseline, round 2)
    q1  What is the code word for alpha? -> A1   [score 1]
```

`m-r00-champion` — исходная версия `v0`; `m-r01-a01` — Кандидат первого
раунда; `m-r01-a01-parent` — повторное измерение исходной версии для сравнения
в одинаковых условиях; `m-r01-final-01-a01` — новый Чемпион `v1` как baseline
второго раунда.

### Рабочая директория кампании

```text
.lab/demo/campaign/
  source/                the agent project the example wrote: agent, launch.json, corpus, scorer, seed
  source/manifest.json   the core's campaign settings: rounds, gate, editable files
  source/editor/         the editor that changes SKILL.md
  source/reflector/      the reflector that explains failures to the editor
  changes.diff           what the campaign changed: v0 -> v1
  export.zip             Lab's export of the champion
```

Также на верхнем уровне лежат `conversations.md`, `report.md` и `summary.json`,
а состояние Lab — в `state/`. **`changes.diff`** содержит изменённую строку
`SKILL.md`. **`export.zip`** — Чемпион, упакованный `lab export` в формате
`lab-change-set/1`: `manifest.json` перечисляет изменённые файлы и их хеши,
`payload/<n>` содержит новые байты. Lab не применяет изменения; перенесите их
в репозиторий агента самостоятельно ([экспорт](../results.md#export-the-champion-as-a-zip)).
**`report.md`** описывает раунды, попытки, причины их завершения и измерения
([отчёты кампании](../results.md#campaign-reports)).

**Продвижение в демо не доказывает качество.** Здесь confidence gate работает в
режиме `observe`: он не применяется, о чём говорит `observe_gate_not_enforced`
в отчёте. В реальной кампании gate применяется ([Запуск кампании](campaign.md)).

## В панели

```sh
just demo-panel              # the evaluation
just demo-panel campaign     # the campaign
```

Команда запускает сервис Lab для чтения состояния демо
(`project/.lab/state-<pair>/` для оценки, `state/` для кампании), затем панель,
и выводит ссылку. Откройте *All experiments* и выберите строку
([экраны панели](../panel.md#step-4-open-the-screens)). Ctrl+C останавливает
панель и сервис. Состояние демо только читается и остаётся на месте.

## Повторный запуск

Каждый запуск заменяет свою рабочую директорию. У двух демо директории разные:
`just demo` не удаляет результаты кампании. `.lab/demo/` можно удалить, когда
она больше не нужна.

При ошибке `just demo` выводит причину и код завершения `lab evaluate`
([exit codes](../first-evaluation.md#exit-codes)); журнал находится в
`project/.lab/runs/001/evaluate.log`. При ошибке `just demo-campaign` выводит
конец журнала примера и сохраняет его как `example.log` в рабочей директории.
См. [устранение неполадок](../install.md#troubleshooting).

## Дальше

- Свой агент: `just new-project ~/my-agent-lab`, затем
  [Connect your agent](../connect-agent.md) — замените заглушку, вопросы и
  оценщик и выполните `just evaluate ~/my-agent-lab`.
- [Run your first evaluation](../first-evaluation.md): вывод `lab evaluate`,
  файлы и коды завершения, затем тот же процесс через Python API.
- [Запуск кампании](campaign.md): собственный манифест, изменяемые файлы,
  Редактор и Рефлектор.
