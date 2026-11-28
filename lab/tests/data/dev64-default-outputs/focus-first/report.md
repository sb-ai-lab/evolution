# Кампания

Run: c-run

Сравнение: unspecified

reflector probe: unavailable (unreadable)
Целостность журнала кампании: complete
Исход ниже относится к прочитанному core префиксу журнала.

Исход core: completed
Причина: rounds_exhausted

Гейт: независимая проверка ядра (семья 15); решение принимает ядро, Lab его не пересчитывает.
Настройки: R 1, режим point, шум исполнения negligible (допуск 0.00, assumed_negligible), проверка reuse, fresh_parent да.

Попытка r01-a01 (раунд 1): исход rejected, причина threshold_not_passed.
- stages: focus rejected · causal not_run
- Правила binding (шлюз, core_raw): focus=point, causal=point.
- сравнение со шлюзом comparison_not_passed (threshold_not_passed), решающий слой focus (gate), против чемпиона раунда · свежий замер, эталон m-r01-a01-pa.
- focus: правило результата point; прирост 0.25; порог > 0.50 → comparison_not_passed
- causal: правило результата point; not_measured
- Условная стоимость в слотах замеров (не деньги): кандидат 1 (гейт 1, финал 0), baseline 2, свежий замер чемпиона: гейт 1, финал 0 (основа: per_reached_finalist); простейший путь к прохождению 4.

Попытка r01-a02 (раунд 1): исход finalist, причина passed.
- stages: focus passed · causal passed
- Правила binding (шлюз, core_raw): focus=point, causal=point.
- Правила binding (глобальное, core_raw): global=point.
- сравнение со шлюзом passed (passed), решающий слой focus (gate), против чемпиона раунда · свежий замер, эталон m-r01-a02-pa.
- focus: правило результата point; прирост 1.00; порог > 0.50 → passed
- causal: правило результата point; прирост 0.00; порог ≥ 0.00
- глобальное сравнение passed (passed), решающий слой global (global), против чемпиона раунда · сохранённый замер, эталон m-r00-champi.
- global: правило результата point; прирост 0.50; порог > 0.00 → passed
- Итоговый балл (eligible_final_score): 1.00
- Условная стоимость в слотах замеров (не деньги): кандидат 2 (гейт 2, финал 0), baseline 2, свежий замер чемпиона: гейт 2, финал 0 (основа: per_reached_finalist); простейший путь к прохождению 5.

Роли: Editor subprocess, Reflector subprocess, Grouper fields.
Проверка подключения: compatible (попыток 1).

Исходная версия: {&#x27;digest&#x27;: &#x27;fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4&#x27;, &#x27;measurement_id&#x27;: &#x27;m-r00-champion&#x27;, &#x27;version&#x27;: &#x27;v0&#x27;}
Текущая версия: {&#x27;baseline_origin&#x27;: &#x27;promoted_attempt&#x27;, &#x27;digest&#x27;: &#x27;777ed7c28835d63ab183f85dcaf8d518ee90a863a3618e2b4945fa4639049e33&#x27;, &#x27;measurement_complete&#x27;: True, &#x27;measurement_id&#x27;: &#x27;m-r01-a02-completion&#x27;, &#x27;version&#x27;: &#x27;v1&#x27;}

| Раунд | Попытка | Исход | Причина | Артефакт | Измерение baseline | Измерение попытки |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | r01-a01 | rejected | threshold_not_passed | artifacts/candidates/r01-a01 | m-r00-champion | m-r01-a01 |
| 1 | r01-a02 | finalist | passed | artifacts/candidates/r01-a02 | m-r00-champion | m-r01-a02 |

Изменения кандидата: см. закреплённые артефакты; текстовый diff не вычислен.

Producing evo-skills: 2.0.0b15

Хеши producing wheels: не переданы.

## Измерение m-r00-champion

Версия: fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4

Покрытие: 2 / 2 (complete).
Попыток исполнения: 2.
Повторы вызовов: 0.
Целостность журнала: complete

| Метрика | Среднее измеренных значений | Число оценок |
| --- | --- | --- |
| score | 0.5 | 2 |

| Сценарий | Повтор | Исполнение | Оценивание |
| --- | --- | --- | --- |
| c1 | 01 | completed | finished |
| c2 | 01 | completed | finished |

## Измерение m-r01-a01

Версия: 1c415578b6bc948b5bf40d41126397c1765d360cfb3a2cd9ec6d8fa124143a7d

Покрытие: 1 / 2 (incomplete).
Попыток исполнения: 1.
Повторы вызовов: 0.
Целостность журнала: complete

| Метрика | Среднее измеренных значений | Число оценок |
| --- | --- | --- |
| score | 0.25 | 1 |

| Сценарий | Повтор | Исполнение | Оценивание |
| --- | --- | --- | --- |
| c1 | 01 | completed | finished |
| c2 | 01 | not_started | not_started |

## Измерение m-r01-a01-parent

Версия: fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4

Покрытие: 1 / 2 (incomplete).
Попыток исполнения: 1.
Повторы вызовов: 0.
Целостность журнала: complete

| Метрика | Среднее измеренных значений | Число оценок |
| --- | --- | --- |
| score | 0 | 1 |

| Сценарий | Повтор | Исполнение | Оценивание |
| --- | --- | --- | --- |
| c1 | 01 | completed | finished |
| c2 | 01 | not_started | not_started |

## Измерение m-r01-a02

Версия: 777ed7c28835d63ab183f85dcaf8d518ee90a863a3618e2b4945fa4639049e33

Покрытие: 2 / 2 (complete).
Попыток исполнения: 2.
Повторы вызовов: 0.
Целостность журнала: complete

| Метрика | Среднее измеренных значений | Число оценок |
| --- | --- | --- |
| score | 1 | 2 |

| Сценарий | Повтор | Исполнение | Оценивание |
| --- | --- | --- | --- |
| c1 | 01 | completed | finished |
| c2 | 01 | completed | finished |

## Измерение m-r01-a02-completion

Версия: 777ed7c28835d63ab183f85dcaf8d518ee90a863a3618e2b4945fa4639049e33

Покрытие: 2 / 2 (complete).
Попыток исполнения: 0.
Повторы вызовов: 0.
Целостность журнала: complete

| Метрика | Среднее измеренных значений | Число оценок |
| --- | --- | --- |
| score | 1 | 2 |

| Сценарий | Повтор | Исполнение | Оценивание |
| --- | --- | --- | --- |
| c1 | 01 | completed | finished |
| c2 | 01 | completed | finished |

## Измерение m-r01-a02-parent

Версия: fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4

Покрытие: 2 / 2 (complete).
Попыток исполнения: 2.
Повторы вызовов: 0.
Целостность журнала: complete

| Метрика | Среднее измеренных значений | Число оценок |
| --- | --- | --- |
| score | 0.5 | 2 |

| Сценарий | Повтор | Исполнение | Оценивание |
| --- | --- | --- | --- |
| c1 | 01 | completed | finished |
| c2 | 01 | completed | finished |

Расход: неизвестен (данные учёта не переданы).
