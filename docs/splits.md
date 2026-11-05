# Train, Validation, And Test Splits

Evolution loads an explicit JSON manifest so training, validation, and reporting use separate task scopes. A split manifest is an input to your run: keep it with the results it produced.

## Runtime contract

A manifest has a content hash, optional source revision, task records, and named scopes:

- `skill_evolution`: training tasks only.
- `dev`: training and validation tasks.
- `reporting`: test tasks only.
- `all`: every task in the manifest.

Record the manifest name and SHA-256 with results. The hash prevents a report from silently mixing manifests that share a display name.

## CLI checks

List the tasks visible in a scope:

```bash
evo split list /path/to/manifest.json --scope skill_evolution
evo split list /path/to/manifest.json --scope reporting
```

Check that supplied task IDs belong to their declared scopes:

```bash
evo split check /path/to/manifest.json \
  --evolution-tasks train-a,train-b \
  --dev-tasks train-a,train-b,val-a \
  --reporting-tasks test-a,test-b
```

`evo run solve` requires `--manifest-scope` when you supply a manifest. `evo evolve reflect` and `evo evolve rewrite` accept only training-scope traces under a manifest.

```bash
evo run solve test-a -m MODEL \
  --manifest /path/to/manifest.json \
  --manifest-scope reporting
```

## Use the scopes correctly

- Diagnose and rewrite on `skill_evolution` only.
- Use validation only to retain or reject a candidate. It must not guide another edit.
- Use `reporting` only after selection. Test evidence must not change skill content, select a candidate, or promote a version.
- Keep the manifest and seeds fixed when comparing models or framework settings.

For the manifest fields and command options, see the [CLI reference](cli.md) and [configuration guide](configuration.md).
