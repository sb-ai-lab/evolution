# Configuration

This page covers environment variables and options for the task/skill commands (`evo task`, `evo run`, `evo evolve`) and their optimizer. For `evo tree evolve`, campaign settings come from a JSON manifest: use the [manifest reference](manifest-v2.md) and [campaign guide](running-campaigns.md). Both workflows store their state in files and need no database.

## Paths

| Variable | Default | Purpose |
|---|---|---|
| `EVO_TASKS_ROOT` | `<cwd>/tasks` | Default task root. An explicit CLI `--dir` or Python `tasks_dir` always wins. |
| `EVO_ROLE_PYTHON_<ROLE>` | unset | Python interpreter for a role-tagged task. |
| `EVO_MLSPACE_ROOT` | implementation default | Base directory used to locate known role environments. |
| `EVO_DISABLE_ROLE_PYTHON` | off | `1` makes role-tagged tasks use the current interpreter. |
| `EVO_PYTHON` | unset | Explicit interpreter used while materializing compatible task workspaces. |

Task-root precedence is therefore:

1. explicit `--dir`/`tasks_dir`;
2. `EVO_TASKS_ROOT`;
3. `tasks/` under the current working directory.

## Provider Credentials

Credentials are accepted from provider environment variables only. The CLI has no credential flag.

| Backend | Environment |
|---|---|
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| GigaChat | `GIGACHAT_CREDENTIALS`; optional `GIGACHAT_SCOPE` |
| Codex, Claude Code, Hermes | The agent CLI's own file-based login/configuration |

`--api-base URL` points LiteLLM at an OpenAI-compatible endpoint. Local endpoints do not require users to provide a dummy credential.

Do not confuse provider credentials with `--pass-env`. Generated programs and evaluators run with a restricted environment. A repeatable `--pass-env NAME` (or Python `pass_env=(...)`) admits only that named variable to child task processes:

```bash
evo run solve my-task -m openai/local-model \
  --api-base http://127.0.0.1:8000/v1 \
  --pass-env HTTPS_PROXY --pass-env SSL_CERT_FILE
```

## LLM Requests

| Variable | Default | Purpose |
|---|---|---|
| `EVO_LLM_MAX_TOKENS` | `16384` | Output-token budget when `--max-tokens`/`max_tokens` is not explicit. Must be a positive integer. |
| `EVO_LLM_REASONING_EFFORT` | unset | Provider reasoning effort such as `low`, `medium`, `high`, or `xhigh`. |
| `EVO_LLM_TEMPERATURE` | backend default | LiteLLM sampling temperature. |
| `EVO_LLM_CALL_TIMEOUT_S` | `900` | LiteLLM call timeout in seconds. |
| `EVO_LLM_TIMEOUT_S` | backend default | Agent CLI request timeout fallback. |
| `EVO_LLM_THINKING_ON` | off | Opt out of automatic local reasoning-model controls. |
| `EVO_PROMPT_RESERVE_TOKENS` | `32768` | Context reserved beyond requested generation tokens when checking a local server limit. |

Max-token precedence is explicit `--max-tokens`/`max_tokens`, then `EVO_LLM_MAX_TOKENS`, then `16384`.

Agent CLI backends separately accept `--permission safe|unsafe` or Python `agent_permission`. `safe` is the default. `unsafe` must be explicit. All safe Hermes calls use file tools only and fail closed unless Linux filesystem containment can start.

## Reflection And Rewrite

| Variable | Default | Purpose |
|---|---|---|
| `EVO_REFLECT_BLIND` | off | `1` hides training verifier/source detail from reflection. Held-back validation evidence is unavailable in either mode. |
| `EVO_REFLECT_DROP_MIXED` | off | `1` omits a failing trace when the same task/version also has a passing trace. |
| `EVO_SOURCE_FEEDBACK` | off | `1` adds bounded, sanitized training execution feedback during collection. |
| `EVO_REWRITE_MODE` | `text-return` | Manual CLI rewrite fallback when no option is explicit: `text-return`, `file-edit`, `agentic-session`, or `edit-ops`. |
| `EVO_OPTIMIZATION_TARGET` | `general` | `general` rejects residual train-literal leakage; explicit `train` permits train-driven content but never held-back evidence. |
| `EVO_REQUIRE_HIGHCONF_REWRITE` | off | `1` keeps the parent when too few reflections are actionable. |

The production optimizer has an explicit `rewrite_mode` default of `edit-ops`; it does not inherit the manual `EVO_REWRITE_MODE` fallback. `agentic-session` requires one task in manual use and is unsupported by the multi-task optimizer.

`EVO_REWRITE_MAX_DELTA_FRAC` is the sole shared base edit-brake budget and defaults to `0.40`. It must be finite and in `[0,1]`. The brake compares finalized non-empty lines after trailing-whitespace normalization and preserves leading whitespace. A base of `0` permits only zero churn. A base of `1` disables churn rejection at promotion count zero. Use `--no-version-decay` to keep that budget for later promotions.

Retained promotions decay the effective budget by `base / sqrt(promotion_count + 1)` to a floor of `min(base, 0.10)`. Promotion count follows retained parent links and does not use a version label, round number, directory count, or attempt count. `EVO_REWRITE_MAX_CHARS` and `EVO_REWRITE_MAX_LINES` remain absolute limits. `EVO_EDIT_BUDGET` limits structured edit operations, not normalized churn. `EVO_EDIT_PROPOSALS` controls structured-edit proposals.

The system warns and ignores `EVO_C1_EDIT_BUDGET_FRAC`, `EVO_REWRITE_MAX_DELTA_MIN_CHARS`, `EVO_REWRITE_MAX_GROWTH`, `EVO_REWRITE_MIN_CHARS`, and `EVO_REWRITE_MIN_LINES`. Use `EVO_REWRITE_MAX_DELTA_FRAC` for the shared brake.

The leak scanner is on by default. `EVO_LEAK_HARNESS_NAMES` adds corpus-specific markers. `EVO_DISABLE_LEAK_SCANNER=1` is a debugging bypass and must not be used when evaluation integrity matters.

## Production Optimizer Policy

The optimizer hashes its logical run definition into durable resume identity. It includes task content, skill trees, bindings, model IDs, and run settings. It also includes rewrite policy, tokens, permissions, manifests, and passed environment names. A partial legacy run without this identity cannot resume.

Promotion uses a fixed conservative policy in 0.2:

- parent and candidate share each validation seed;
- arm order is deterministically counterbalanced;
- at least four validation pairs are required;
- any per-task mean regression rejects the candidate;
- the paired sign-flip gate uses `alpha=0.10`.

Generic research gate/selection environment variables do not reconfigure this production policy. Validation checkpoints persist scalar `seed`, `passed`, `total`, and `pass_rate` values only.

## Traces And Auditing

| Variable | Default | Purpose |
|---|---|---|
| `EVO_TRACE_RETENTION` | `0` | Maximum traces per skill; `0` disables pruning. |
| `EVO_PERSIST_PROMPT_AUDIT` | on | `0` disables the workspace `prompt_audit.json`. |
| `EVO_TRACE_ARTIFACT_MAX_FILES` | implementation default | Maximum artifact entries captured in trace evidence. |
| `EVOLUTION_WORKSPACE_GUARD_LOG_ALLOW` | off | `1` logs allowed as well as blocked workspace-guard events. |
| `EVOLUTION_GUARD_STRIP_PATH_DIRS` | `/home/user/conda` | Host path entries removed from child `PATH`. |

Variables beginning `EVOLUTION_WORKSPACE_GUARD_`, plus `TASK_WORKSPACE`, `WORKSPACE_DIR`, and `SB_OUTPUT`, are normally set by the framework for child processes. Applications should not set them manually.

## Repository Research Configuration

The source checkout has optional `experiments/` runners with additional variables and dependencies. They are not part of the installed wheel or production optimizer contract. Use the selected experiment runbook instead of treating these variables as core settings.

## Campaign role environment

Tree campaigns bind the environment declared by each role in the manifest. The CLI uses its process environment as the base; library callers can supply an explicit mapping. Declared `env_passthrough` names are selected from that base for the role process. Put credential values in the environment, never in the manifest. Missing declared credentials are refused before role execution.

Relative program and documentation paths resolve from the manifest directory. Executables are resolved using the admitted command environment, and the executable/interpreter identity is bound for resume. A symlinked virtual-environment interpreter remains a valid declaration; changing its resolved executable or a declared role program can require accepting the reported identity change. Changing the secret value is not a way to alter an already bound program or comparison policy.

The separate task/skill commands use `--pass-env` for generated code and evaluators; that option is not a tree-role declaration. See [child-process environment](child-process-environment.md) and the [manifest command contract](manifest-v2.md) for the respective controls.
