# Обзор примеров

[English](../examples.md) · Русский

**Цель:** увидеть полный минимальный пример агента, подключённого к Lab, и
разобраться в назначении файлов до подключения своего агента. Lab поставляется
с двумя работающими примерами. Оба выполняются офлайн, без модели, сети и
ключей, и завершаются за несколько секунд. Эта страница не повторяет команды
запуска: она объясняет состав примеров и куда смотреть.

Из клона репозитория `just demo` и `just demo-campaign` запускают примеры и
сохраняют рабочие пространства в `.lab/demo/`; страница [Демо](demo.md)
объясняет их вывод.

| Пример | Что делает | Команда и ожидаемый вывод |
|---|---|---|
| [`examples/owner/evaluation.py`](../../../lab/examples/owner/evaluation.py) | Измеряет агента-заглушку из шаблона через явные вызовы Owner: Bundle → conformance → фиксированная оценка → отчёт | [Установка, шаг 5](install.md#шаг-5-запустите-офлайн-пример) |
| [`examples/owner/campaign.py`](../../../lab/examples/owner/campaign.py) | Даёт Core улучшить агента: Bundle → check → кампания → отчёт → diff → экспорт | [Запуск кампании, шаг 1](campaign.md#шаг-1-запустите-офлайн-пример-кампании) |

Оба примера написаны для текущей пары версий: `evolution-lab 0.0.3.dev81` и
строго `evo-skills 2.0.0b22`. Остальные примеры в
[`examples/`](../../../lab/examples/README.md) относятся к старым парам и
сохранены только для чтения.

## В каком порядке смотреть

1. **Шаблон проекта.** `lab new-project DIR` создаёт его, а `lab evaluate DIR`
   измеряет. Это отправная точка для своего агента: агент и адаптер в отдельных
   файлах, корпус и оценщик. `README.md` описывает необходимые правки.
2. **`evaluation.py`** измеряет тот же шаблон явными вызовами Owner вместо
   `lab evaluate`. Читайте его для управления Lab из своего кода.
3. **`campaign.py`** добавляет Редактор и Рефлектор, чтобы Core могло менять
   файлы агента и сохранять изменения при подходящей оценке. Пример использует тот же шаблон проекта с профилем observe и синтетическим агентом, которому нужна правка примера для правильного ответа.
4. **Более реалистичный агент** — ассистент поиска заказов с одним инструментом
   в разделе [Подключение агента](connect-agent.md#полный-пример-ассистент-поиска-заказов).
   Он показывает настройки, инструменты и отображение ошибок в исходы хода.

## Пример оценки

> **Новое в 0.0.3.dev72.** Пример создаёт проект из шаблона `lab new-project`.
> В 0.0.3.dev71 и ранее он использует заготовку Core; см. эту страницу под
> тегом `v0.0.3.dev71`.

### Что он записывает

Скрипт записывает шаблон в `--source-directory`, заполняет `"lab:auto"` в
копии запуска, фиксирует копию в Bundle внутри `--state-directory` и управляет
ей через Python Owner. Это тот же проект, который создаёт `lab new-project`:

```text
example-source/
  agent/my_agent.py        the agent: a stub without a model; in your project, your agent
  agent/adapter.py         the adapter Lab loads: build(context) and five methods that call MyAgent
  seed/skills/SKILL.md     the prompt the adapter reads from the measured copy
  corpus.jsonl             three cases, q1 to q3: public question and oracle.expected
  scorer/score.py          reads one sample on stdin, prints 1.0 if the answer equals the expected text
  launch.json              how Lab starts the adapter (lab-target-launch/3, "lab:auto" fields)
  evaluation-input.json    what to measure: corpus, scorer, metric, seeds (lab-evaluation-input/3)
  README.md                what to edit, field by field
  .lab/run/                the run copy with "lab:auto" filled in; the Bundle is made from it
```

`just demo` использует тот же шаблон через `lab new-project` и `lab evaluate`;
[Демо](demo.md#агент-адаптер-и-что-нужно-заменить) объясняет, что заменять.

### Агент и адаптер

Шаблон разделяет их. Агент `agent/my_agent.py` ничего не знает о Lab:

```python
class MyAgent:
    def __init__(self, prompt: str) -> None:
        # The prompt is seed/skills/SKILL.md as Lab hands it over. The stub uses
        # it for one thing: whether to answer with the code word alone.
        self._terse = "code word only" in prompt.lower()

    def answer(self, question: str) -> str:
        words = question.lower().replace("?", " ").split()
        code = next((_KNOWN[word] for word in words if word in _KNOWN), "unknown")
        return code if self._terse else f"The code word is {code}."
```

`_KNOWN` содержит кодовые слова для `alpha` и `beta`; `gamma` намеренно
отсутствует, чтобы показать оценки 1 и 0. Промпт содержит «code word only»,
поэтому заглушка отвечает `A1`; без этой фразы ответ `The code word is A1.`
не пройдёт проверку оценщика на точное совпадение.

Lab импортирует адаптер `agent/adapter.py`:

- `build(context)` — точка входа в `launch.json` (`"agent": {"module": "adapter", "attribute": "build"}`); её импортирует запускатель Lab, HTTP-сервер писать не нужно.
- `factory(view)` создаёт агента для экземпляра, читая `skills/SKILL.md` из
  `view.root` — измеряемой копии `seed/` — и передавая его в `MyAgent`.
- `open` начинает сессию и отмечает conformance, `turn` отвечает фиксированным
  текстом для conformance или вызывает `MyAgent.answer`, а `close_session` и
  `aclose` освобождают ресурсы.
- Ошибки агента становятся исходами `candidate_failed` или `target_failed` с
  коротким кодом причины.
- `describe()` возвращает сведения агента о себе, которые не должны содержать секретов.

[Подключение агента](../connect-agent.md) объясняет методы, исходы и необходимые правки.

### Что вызывает скрипт

- `_prepare` создаёт проект и фиксирует его: `new_project`,
  `describe_environment`, `resolve_project` (заполнение `"lab:auto"`),
  `PreparationStore.create_bundle` и `prepare_evaluation` со всеми кейсами.
- `_run` открывает Owner и выполняет `bind` → `start_target` → `conformance`
  (останавливает Target Service) → второй `start_target` → `start` оценки → `report`
  → `stop_target`.

[What `lab evaluate` does inside](../first-evaluation.md#what-lab-evaluate-does-inside)
разбирает вызовы по порядку. `lab evaluate` делает то же и добавляет секреты,
файлы запуска и вывод. Digest идентичностей в копии запуска — значения по
умолчанию, которые `resolve_project` вычисляет из объявления запуска; см.
[Identity digests](../connect-agent.md#choose-and-record-the-identity-digests).

## Пример кампании

### Что он записывает

```text
campaign-source/
  agent/my_agent.py        synthetic demo: answers correctly after the sample edit
  agent/adapter.py         the ordinary skeleton adapter
  evaluation-input.json    fixed measurement of the same project
  campaign-launch.json     campaign target launch
  launch.json              evaluation target; policy is evaluation-input.json
  manifest.json            Core's campaign manifest (evolution-v2-manifest/15)
  corpus.jsonl             the cases
  scorer/score.py          compares the whole answer with oracle.expected
  editor/edit.py           Core's sample Editor: rewrites the first line of SKILL.md
  reflector/reflect.py     Core's sample Reflector: diagnoses failing cases for the editor's brief
  seed/skills/SKILL.md     the file the campaign is allowed to change
```

Кампания использует тот же шаблон `lab new-project`. Демо заменяет заглушку синтетическим агентом: он знает все три кодовых слова, но возвращает их только после появления `IMPROVED:` в измеряемом skill. Адаптер, корпус, оценщик и примеры ролей остаются прежними. Gate observe относится только к этому бесплатному демо соединений.

### Что происходит

Первая строка seed получает `0.0`. Пример Редактора меняет её на
`IMPROVED: reference skill`, агент начинает отвечать ожидаемыми кодовыми словами, а оценщик
выставляет `1.0`. Раунд 1 заканчивается с `promoted`, Чемпионом становится
`v1`; во втором раунде улучшать больше нечего, и он заканчивается с `clean`.
Затем `diff` и `export` показывают изменённый файл и позволяют его забрать.

Скрипт преобразует манифест заготовки в smoke-профиль Lab
(`campaign.gate.mode: "observe"`): gate не применяется, поэтому продвижение
показывает работоспособность соединений, а не улучшение качества. Чтобы
получить подтверждение качества, изучите настройки gate в разделе
[Запуск кампании](campaign.md).

### Что вызывает скрипт

`_prepare` создаёт шаблон проекта, применяет профиль демо и разрешает `campaign-launch.json`,
затем вызывает `create_bundle` и `prepare_campaign`. `_run` проходит через
Owner: `bind` → `start_target` → `check` → `start` → ожидание кампании →
`report` → `diff` → `export` → `stop_target`. Шаги 7–10 раздела
[Запуск кампании](campaign.md) выполняют те же вызовы с вашими входами.

## Как читать вывод

Каждый скрипт выводит одну сводку JSON. Проверяемые поля и их ожидаемые значения
описаны в разделах [Установка: как выглядит успех](install.md#как-выглядит-успех)
для оценки и [Запуск кампании: как выглядит завершённая кампания](campaign.md#как-выглядит-завершённая-кампания)
для кампании. Отчёты, строки корпуса и диалоги остаются приватными в каталоге
состояния.

## Результаты в панели

Примеры не запускают [панель](panel.md): они вызывают библиотеку Owner
напрямую и завершаются после работы. Завершённый прогон остаётся в каталоге
состояния, поэтому его можно открыть позже. После вывода сводки скриптом
запустите из `lab/` после `just setup` сервис поверх состояния примера — переданного вами
`--state-directory` (здесь `campaign-state`, как в разделе
[Запуск кампании](campaign.md#шаг-1-запустите-офлайн-пример-кампании)):

```bash
.lab/venv/bin/lab serve --home "$PWD/lab-home" --state "$PWD/campaign-state"
```

Во втором терминале, также из `lab/` после `just setup`:

```bash
.lab/venv/bin/lab panel --home "$PWD/lab-home" --open
```

Оба пути должны быть абсолютными, поэтому используется `$PWD`. Откройте *All
experiments* и нажмите строку кампании; экраны описаны в разделе
[Панель Lab, шаг 4](panel.md#шаг-4-откройте-экраны). Чтобы вместо этого
посмотреть оценку, передайте её каталог состояния, например
`--state "$PWD/lab-state-example"` из раздела
[Установка](install.md#шаг-5-запустите-офлайн-пример). Остановите панель,
а затем сервис с помощью Ctrl+C. Здесь сервису не нужен `--execution-wheel`:
он только читает завершённые прогоны.

## Перед запуском

- Запускайте из каталога `lab/` после `just setup` ([Установка](install.md)),
  и передавайте один и тот же интерпретатор для запуска скрипта и в `--python`.
  Путь `--python`, содержащий `..`, например `../lab/.lab/venv/bin/python`, приводит
  к `environment_invalid`.
- `--state-directory` и `--source-directory` ещё не должны существовать.
  После неудачного прогона используйте новые имена.
- `--port` должен быть свободен на `127.0.0.1`. Там во время скрипта слушает
  агент-заглушка для Core; это не порт панели.

## Что дальше

- [Первое измерение своего агента](../first-evaluation.md#measure-your-own-agent)
  — замените агента примера, корпус и оценщик своими.
- [Подключение агента](connect-agent.md) — полный контракт хода и файл запуска.
- [Запуск кампании](campaign.md) — собственный манифест, изменяемая область,
  Редактор и Рефлектор.
