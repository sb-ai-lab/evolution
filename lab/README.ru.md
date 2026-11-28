# Evolution Lab

[English](README.md) · Русский

Этот каталог — пакет `evolution-lab`, рекомендуемый способ запускать
Evolution. Lab готовит воспроизводимый эксперимент по улучшению LLM-агента,
запускает его на движке (`evo-skills`, остальная часть этого репозитория),
честно измеряет результат и позволяет забрать полученные изменения.

```text
evolution-lab 0.0.3.dev81  +  evo-skills 2.0.0b22 (exact)   ·   Python ≥ 3.11.4   ·   Linux или macOS
```

[Пара версий и установка](docs/RELEASES.md).

**Пользуетесь Lab?** Начните с [README](../README.ru.md) и
[быстрого старта](../QUICKSTART.ru.md) репозитория. [Руководство
пользователя](../docs/guide/ru/README.md) объясняет каждую задачу, а
[Как устроен Lab](../docs/guide/ru/concepts.md) вводит термины. Эта страница —
для работы над самим пакетом.

На macOS процесс может пережить остановку, если выйдет из группы, исчезнет из дерева между опросами и скроет метку окружения.

## Быстрый старт

На Linux или macOS с `uv`, `just` и `git` (полный список — в
[Установке](../docs/guide/ru/install.md)):

```sh
git clone https://github.com/sb-ai-lab/evolution.git
cd evolution/lab
just setup                         # wheel ядра и Lab в .lab/venv
just demo                          # офлайн-оценка: без LLM и ключей
just new-project ~/my-agent-lab    # каркас проекта для вашего агента
just evaluate ~/my-agent-lab       # его первый замер
```

`just setup` собирает wheel ядра из закоммиченных файлов репозитория по
последнему коммиту, изменившему что-либо вне `lab/`, собирает wheel Lab из
этого checkout и ставит оба в `.lab/venv`. Editable-установка
(`pip install -e .`) не работает: Lab запускает ядро и вашего агента в
изолированных подпроцессах `python -I` и сверяет установленные файлы с wheel.
`just` выводит все рецепты.


## Выпуск

[Выпуски](docs/RELEASES.md) перечисляют версии в этом снимке.

## Структура

```text
src/evolution_lab/
  preparation/   Bundle и подготовленное выполнение
  owner/         Python Owner: кампания, оценка, conformance, результаты
  target/        протокол ходов TurnAgent и target launcher
  service/       локальный сервис на Unix-сокете (extra [service])
  cli/           команда `lab`
  panel/         необязательная браузерная панель
  guides/        встроенные руководства и каталоги для агентов, едут в wheel
  observations/  импорт локальных трасс
  projects/      дом Lab, реестр проектов, демо-проект
  quickstart/    каркас проекта, lab new-project и lab evaluate
  client.py      клиент одной кампании
  budget.py      бюджетный шлюз (extra [gateway])
  runner.py, recipe.py, report.py, evaluation.py, standalone.py   устаревший путь
tests/           pytest
examples/        агентно-нейтральные примеры, см. examples/README.md
docs/            справочник API
                 (руководства пользователя — в ../docs/guide/)
```

- [Справочник API](docs/api/): точные формы, состояния и коды ошибок.
