# Use Lab from the command line

Follow the [CLI walkthrough](../../../docs/guide/cli.md#walkthrough-evaluate-the-order-lookup-assistant)
to prepare a Bundle, start the synthetic order-lookup target, run a fixed
evaluation and read its report. It uses Lab `0.0.3.dev60` / exact core `2.0.0b12` with fresh pair state.
The intended release identifier is `v0.0.3.dev60`; delivery status and source
provenance are in [RELEASES](../../docs/RELEASES.md). Keep old runs with their
producing wheels.

The walkthrough explains the request files, which IDs to keep from each
response, and how to stop the target. With its three cases and two repeats,
a complete evaluation has six scored samples. Read coverage and cleanup
alongside the scores.

This directory has no runnable script: each service action is a CLI command
you issue explicitly. Starting `lab serve` alone starts no target or evaluation.
For exact request shapes and error codes, use the
[service API reference](../../docs/api/service-s5.md).
