# Security Policy

## Supported versions

Security fixes cover the latest publicly published beta. A newer private development build does not end support for that public beta. Older public betas are unsupported; upgrade to the latest published beta when reporting a vulnerability. Stable-release support will be documented when a stable release is published.

## Trust model

Evolution is a local CLI for one trusted operator. Generated solution code, task setup scripts, and task tests execute as subprocesses with the operator's host account. The workspace guard reduces accidental writes and records what happened; it is **not an operating-system security sandbox**.

Do not expose Evolution as a multi-user service. Do not run tasks, skills, model output, or archives from an untrusted source. Use an external container or virtual machine for untrusted code.

Safe agent-CLI mode limits the arguments, environment, and workspace supplied to supported Codex and Claude Code installations. Those controls do not turn the third-party CLI into a security boundary. `--permission unsafe` deliberately disables those protections and must not be used with untrusted input. Hermes does not implement safe mode and therefore fails closed unless unsafe mode is requested explicitly.

Prompts, model responses, generated code, evaluator output, and trace metadata may contain confidential task data. Keep `.agents/`, `.traces/`, optimizer run state, and local environment files out of version control and release archives.

The process logs that `evo tree evolve` and `evo tree evaluate` write under a run directory's `logs/` are owner-only and redact the run's secret values from copied terminal text. A `--log-file` copy from `evo tree conformance` or `evo-target serve` is private but not redacted, because those commands have no run secret set. Read any log before sharing it.

## Reporting a vulnerability

Use the repository Security tab to report vulnerabilities privately. If private reporting is unavailable, open an issue requesting a private contact channel without including exploit details.

Include:

- A clear description of the vulnerability and impact.
- Minimal reproduction steps.
- The affected version from `pip show evo-skills` or `evo --version`.
- A suggested fix, if available.

## In scope

- Path traversal or symlink escape in task, skill, container, trace, import, or workspace handling.
- Credentials inherited by generated-code, verifier, generator, or agent-CLI subprocesses without explicit opt-in.
- Secrets written into logs, JSON output, traces, or optimizer state.
- Unsafe archive extraction or deserialization.
- State corruption that can activate different skill content than the recorded immutable version.
- Safe agent mode silently falling back to an unsafe invocation.
- TLS verification being disabled without an explicit development override.

## Out of scope

- Isolation of deliberately executed untrusted code; that deployment is not supported by this project.
- Behavior or availability of third-party model providers and agent CLIs.
- Cost or quota exhaustion caused by an operator-selected model, task count, trial count, or unsafe mode.
