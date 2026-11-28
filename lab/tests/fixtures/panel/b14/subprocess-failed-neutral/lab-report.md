# Кампания

Run: c-run

Сравнение: unspecified

reflector probe: unavailable (unreadable)
Целостность журнала кампании: complete
Исход ниже относится к прочитанному core префиксу журнала.

Исход core: терминального исхода core нет
Причина: —

Гейт: независимая проверка ядра (семья 15); решение принимает ядро, Lab его не пересчитывает.
Настройки: R 1, режим point, шум исполнения negligible (допуск 0.00, assumed_negligible), проверка reuse, fresh_parent нет.

round 1: probe mediator failed to start; no probe calls
Роли: Editor subprocess, Reflector subprocess, Grouper fields.
Проверка подключения: compatible (попыток 1).

Кампания приостановлена: reflector_failing (владелец reflector, evidence stage_record).
Reflector раз за разом падает вне своего диагноза (таймаут, сбой процесса, сеть, ресурсы хоста, логин CLI агента) либо две группы ошибок подряд пропущены после повторных сбоев по содержанию; после внешнего или временного сбоя устраните его и продолжайте; после повторных пропусков по содержанию продолжение поможет, только если сбои были временными, иначе нужно менять программу, промпт, лимиты Reflector или max_consecutive_failures — это новый Bundle и новая кампания
Продолжение средствами Lab в этой версии недоступно; папка прогона сохранена.

| Запись | Причина | Владелец | Evidence |
| --- | --- | --- | --- |
| 15 | reflector_failing | reflector | stage_record |
Окно неопределённости: {&quot;counts&quot;: {&quot;environment&quot;: 0, &quot;rollout&quot;: 0, &quot;scorer&quot;: 0, &quot;target&quot;: 0}, &quot;denominator&quot;: 0, &quot;start&quot;: 15}

Исходная версия: {&#x27;digest&#x27;: &#x27;fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4&#x27;, &#x27;measurement_id&#x27;: &#x27;m-r00-champion&#x27;, &#x27;version&#x27;: &#x27;v0&#x27;}
Текущая версия: {&#x27;baseline_origin&#x27;: &#x27;seed&#x27;, &#x27;digest&#x27;: &#x27;fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4&#x27;, &#x27;measurement_complete&#x27;: True, &#x27;measurement_id&#x27;: &#x27;m-r00-champion&#x27;, &#x27;version&#x27;: &#x27;v0&#x27;}

| Раунд | Попытка | Исход | Причина | Артефакт | Измерение baseline | Измерение попытки |
| --- | --- | --- | --- | --- | --- | --- |

Раунды без попыток:

| Раунд | Исход | Причина | Класс ошибки рефлексии |
| --- | --- | --- | --- |
| 1 | — | — | reported |

Изменения кандидата: см. закреплённые артефакты; текстовый diff не вычислен.

Producing evo-skills: 2.0.0b14

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

Расход: неизвестен (данные учёта не переданы).
