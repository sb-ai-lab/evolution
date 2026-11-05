# Importing SB-Bench Tasks

Evolution can convert tasks from a local SB-Bench checkout into its own task format. SB-Bench is an external corpus, not an installed dependency or bundled dataset.

## What The Importer Does

For each matching `tasks/<role>/<task>/` source directory, the importer:

1. parses the source `task.toml` and discovers its reusable skills;
2. runs `data_generator.py` when the task provides one;
3. creates a flat Evolution task named `sb-<role>-<short-name>`;
4. rewrites source data paths in `instruction.md`;
5. copies `environment/data/` into `inputs/`;
6. copies tests, rewrites `SB_OUTPUT` to `TASK_WORKSPACE`, and places ground-truth files under the imported `tests/` directory;
7. optionally imports required skills and creates a skill container.

The import is a compatibility conversion, not a security boundary. Review the resulting task before running model-generated code or tests from an untrusted checkout.

## Commands

Import every discovered task:

```bash
evo task import-sb-bench /path/to/sb-bench
```

Filter by role, difficulty, or a comma-separated list of task ID prefixes:

```bash
evo task import-sb-bench /path/to/sb-bench --role de
evo task import-sb-bench /path/to/sb-bench --difficulty easy
evo task import-sb-bench /path/to/sb-bench --tasks de/E1,de/E2
```

Import the selected tasks, their skills, and matching containers:

```bash
evo task import-sb-bench /path/to/sb-bench \
  --tasks de/E1,de/E2 \
  --with-skills --with-containers
```

Use `--force` to replace an existing imported task and `--dir PATH` to choose the destination task root. Without `--dir`, task discovery follows the normal resolution order: `EVO_TASKS_ROOT`, then `tasks/` below the current directory.

The command does not provide `--all`, `--no-generate`, or `--dry-run` options. Omit all filters to select every discovered task.

## Naming And Metadata

An SB-Bench ID such as `de/E1-duckdb-assembly` becomes `sb-de-duckdb-assembly`. The converted metadata records the source ID, role, difficulty, category, tools, and tags. Skill directories retain their source names; duplicate names are imported once.

## Verification

Inspect the imported surface before a solve:

```bash
evo task list
evo skill list
evo skill container list
evo doctor -m mock --task sb-de-duckdb-assembly
```

Run the task only after its Python and system dependencies are available in the host environment:

```bash
evo run solve sb-de-duckdb-assembly -m MODEL
```

Evolution runs task setup, generated code, and pytest on the host. Corpus-specific libraries are not part of `evo-skills`. Install them in a dedicated environment. You can also run the repository in an external container or virtual machine.

## Importing SkillsBench Skills

The separate SkillsBench skill importer is grouped under `evo skill`:

```bash
evo skill import-skillsbench /path/to/skillsbench
evo skill import-skillsbench /path/to/skillsbench --skills pdf,xlsx
evo skill import-skillsbench /path/to/skillsbench --theme document
```

The importer preserves the source task identifier in each imported skill's metadata and skips duplicate skill names.
