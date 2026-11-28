# Кампания

Run: c-run

Сравнение: unspecified

reflector probe: unavailable (unreadable)
Целостность журнала кампании: complete
Исход ниже относится к прочитанному core префиксу журнала.

Исход core: completed
Причина: rounds_exhausted

**Ни один кандидат не замерен** (уведомление Lab, итог остаётся словом ядра): incomplete_sample — 1.

Гейт: независимая проверка ядра (семья 15); решение принимает ядро, Lab его не пересчитывает.
Настройки: R 1, режим point, шум исполнения negligible (допуск 0.00, assumed_negligible), проверка reuse, fresh_parent да.

Попытка r01-a01 (раунд 1): исход insufficient_evidence, причина incomplete_sample; сравнения нет.
- Предварительная проверка: not_ruled_out (unknown_fresh_reference_envelope): focus: наибольший прирост 1.00; порог > 0.00 (может пройти); causal: наибольший прирост 1.00; порог ≥ 0.00 (может пройти).
- Правила binding (шлюз, core_raw): focus=point, causal=point.
- Условная стоимость в слотах замеров (не деньги): кандидат 2 (гейт 2, финал 0), baseline 2, свежий замер чемпиона: гейт 2, финал 0 (основа: per_reached_finalist); простейший путь к прохождению 5.

Роли: Editor subprocess, Reflector subprocess, Grouper fields.
Проверка подключения: compatible (попыток 1).

Исходная версия: {&#x27;digest&#x27;: &#x27;fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4&#x27;, &#x27;measurement_id&#x27;: &#x27;m-r00-champion&#x27;, &#x27;version&#x27;: &#x27;v0&#x27;}
Текущая версия: {&#x27;baseline_origin&#x27;: &#x27;seed&#x27;, &#x27;digest&#x27;: &#x27;fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4&#x27;, &#x27;measurement_complete&#x27;: True, &#x27;measurement_id&#x27;: &#x27;m-r00-champion&#x27;, &#x27;version&#x27;: &#x27;v0&#x27;}

| Раунд | Попытка | Исход | Причина | Артефакт | Измерение baseline | Измерение попытки |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | r01-a01 | insufficient_evidence | incomplete_sample | artifacts/candidates/r01-a01 | m-r00-champion | m-r01-a01 |

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

Версия: b27e97ab81c8295da8b393432248fdac496e59dfbb9bcd9ad802e2971e6407c7

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

## Измерение m-r01-a01-parent

Версия: fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4

Покрытие: 1 / 2 (incomplete).
Попыток исполнения: 3.
Повторы вызовов: 0.
Целостность журнала: complete

| Метрика | Среднее измеренных значений | Число оценок |
| --- | --- | --- |
| score | 1 | 1 |

| Сценарий | Повтор | Исполнение | Оценивание |
| --- | --- | --- | --- |
| c1 | 01 | target_failed | not_started |
| c2 | 01 | completed | finished |

Расход: неизвестен (данные учёта не переданы).
