# Preparation S1: нейтральный caller flow

Этот walkthrough относится к исторической паре dev7/a8. Сохраняйте его Bundle
и state вместе с исходным окружением. Для текущей пары начните с
[первой оценки](../../../docs/guide/first-evaluation.md): она объясняет подготовку,
запуск и чтение результата без editor/reflector.

`prepare.py` принимает пути существующей интеграции и показывает только
подготовку:

1. описывает caller execution venv;
2. при необходимости описывает отдельный target venv;
3. создаёт новый launch file с точными descriptor digest и Python executable;
4. создаёт Bundle и PreparedExecution;
5. открывает обе сводки через новый `PreparationStore`.

Пример не содержит agent, corpus, prompts, credentials или target launcher и не
запускает target, campaign, H1, readiness или conformance. Запускайте script тем
POSIX venv Python 3.11+, который должен стать `environments[0]`. Передайте точные
сохранённые wheel archives установленной пары `evolution-lab`/`evo-skills` и всех
других объявляемых зависимостей; пример ничего не устанавливает.

```sh
/work/venv/bin/python examples/preparation_s1/prepare.py \
  --manifest /work/campaign/manifest.json \
  --launch-template /work/config/launch.template.json \
  --launch-output /work/campaign/launch.s1.json \
  --state-directory /work/lab-state-dev7-a8-new \
  --root project=/work/campaign \
  --execution-wheel /archives/evolution_lab-0.0.3.dev7-py3-none-any.whl \
  --execution-wheel /archives/evo_skills-0.3.0a8-py3-none-any.whl \
  --env MODE=offline \
  --secret-name TARGET_TOKEN
```

Команда показывает test-only dev7/a8 snapshot, а не опубликованную или
интегрированную поставку. Передавайте точные сохранённые архивы установленной
producing-пары. `PreparationStore` и последующий `Owner` должны использовать
один новый private state root для dev7/a8. Не открывайте им прежний dev6/a5 root
и не копируйте в новый каталог старую SQLite DB или cleanup receipts.

`--launch-output` обязателен, должен отсутствовать и находиться ровно в одном
`--root`. Template обязан быть вне всех roots, поэтому его временные поля и
metadata случайно не попадут в Bundle. Script копирует template в выбранный
output, явно заменяет `environment_ref`, `command.executable`, `env` и
`secret_names`, но не переписывает остальные launch поля. После ошибки новый
output остаётся caller-owned source file; выберите другой путь или осознанно
проверьте его перед повтором.

Manifest должен быть native `/4`; его role programs используют `edit_brief/4`
и `reflect_brief/2`. Все manifest paths, scripts, policy, corpus, seed и прочие
inputs должны уже находиться в объявленных roots. Дополнительные материалы
объявляются отдельными `--root`. Если интеграции нужны нестандартные explicit
`PathBinding`, используйте Python API напрямую.

Для command grouping объявите executable, cwd, dependency paths, allowed env и
файловые argv operands. Поле `config` является opaque mapping: Lab не ищет и не
переписывает в нём строки, похожие на пути. Объявление dependency не исправляет
абсолютный locator внутри `config`; интеграция сама обеспечивает корректные
ссылки в сохранённом layout.

Если задан `environment_docs`, Lab сохраняет полный отсортированный inventory
каждого regular UTF-8 файла и его SHA-256. Поле
`PreparedInputs.environment_docs_digest` — SHA-256 compact UTF-8 JSON вида
`{"schema":"lab-doc-inputs/1","files":[{"path":...,"sha256":...}]}`.
Это provenance Lab, не core snapshot digest и не прежний digest обрезанного
rendered текста; сравнивать эти значения между форматами нельзя.

Probe config `/4` сохраняется как native semantics, но preparation не вызывает
probe. Оператор владеет отдельным disposable probe service и его mutable state;
Owner cleanup target/campaign не очищает probe state.

Для отдельного target добавьте его interpreter и retained wheels:

```sh
  --target-python /work/target-venv/bin/python \
  --target-wheel /archives/target_runtime-1.2.3-py3-none-any.whl
```

Без `--target-python` launch выбирает caller execution environment. Runtime files
объявляются через `--execution-runtime-file` или `--target-runtime-file`; их,
venv и wheel archives нельзя помещать внутрь roots. `--env NAME=VALUE` принимает
только несекретные значения. `--secret-name NAME` сохраняет только имя; secret
values этому примеру не передаются.

`describe_environment` и `create_bundle` каждый выполняют отдельную проверку
`<declared-python> -I -S` только stdlib verifier. Prepare/get читают сохранённые
данные. Успешная подготовка не является runtime attestation: уже загруженный
caller code после in-place upgrade и будущий target import resolution через
`.pth`, `PYTHONPATH`, hooks или editable external source остаются ограничениями.
Owner выполняет свои runtime rechecks перед dispatch. В dev8/a8 explicit
conformance и fixed evaluation находятся в отдельном S3 пути; успешная S1
подготовка сама по себе их не запускает и не доказывает.
