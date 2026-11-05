# Child-process environment

Evolution runs generated solutions and evaluators as child processes under your host account. It is not an operating-system sandbox. These children receive an explicit environment so provider credentials and unrelated host state do not silently enter task execution.

## Default variables

When present, these runtime variables are forwarded:

```text
PATH LANG LC_ALL LC_CTYPE TZ TERM TMPDIR
PYTHONPATH PYTHONIOENCODING PYTHONUTF8
VIRTUAL_ENV CONDA_PREFIX LD_LIBRARY_PATH
SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE
```

All other names are absent by default, including provider credentials. An absent name stays absent rather than becoming an empty string. Trusted utility processes and in-process framework setup can have different environments; this list describes generated solution and evaluator children.

## Forward another variable

Use a repeatable `--pass-env NAME` option on the task/skill commands when a child needs an additional variable. For example, `--pass-env HTTPS_PROXY --pass-env SSL_CERT_FILE` supplies the named network settings. Naming a provider credential explicitly also forwards that credential; select only the names the task needs.

Library callers can pass an explicit base environment and `allow` collection to `evolution.eval.child_env.minimal_child_env`. Campaign roles have their own manifest declarations; see [campaign role environment](configuration.md#campaign-role-environment).

## Choose the Python import path deliberately

`PYTHONPATH` is forwarded when it is set. Choose it before launching Evolution, and verify that it names the intended installed package or source checkout. A different import path can change which code a task executes and make two measurements incomparable. Importing Evolution does not rewrite that environment variable.

See [Configuration](configuration.md), the [CLI reference](cli.md), and [Security](../SECURITY.md) for environment precedence, permissions and the host-account trust boundary.
