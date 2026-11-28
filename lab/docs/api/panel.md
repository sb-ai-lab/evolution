# Panel API: `lab panel`, its forms and the forwarded routes

> **Who this is for.** Anyone who runs `lab panel` from a script, or needs to know
> exactly what the panel answers and forwards. For the guided steps read
> [The Lab panel](../../../docs/guide/panel.md); for the service the panel reads, the
> [local service reference](service-s5.md).

The panel is a read-only local gateway: an ASGI app listening on `127.0.0.1`,
addressed by a per-launch host name `p<16 hex>.localhost`, that serves
a static page and forwards an allowlist of the service's `GET` routes over the service's
Unix socket. It does not read the Lab home or interpret service JSON. Available from Lab
`0.0.3.dev31`, extra `evolution-lab[panel]`.

## Commands

```text
lab panel [--home H] [--socket S] [--port N] [--open]       foreground
lab panel [--home H] [--socket S] [--port N] --background [--open]
lab panel status [--home H]
lab panel stop [--home H] [--wait-seconds N]                 default 10
```

`--port 0` (the default) takes any free port. `--open` opens the printed link in the
default browser. `python -m evolution_lab.cli panel …` is equivalent to `lab panel …`.
With the global `--json` the output is the JSON forms below; otherwise one text line.
Exit code 0 on success; 1 with a [CLI error](#cli-errors) on stderr.

## Liveness, files and lock

Liveness is the lock, not the PID: a running panel holds an exclusive `flock` on
`<home>/panel.lock` for its whole life, so a free lock means no panel whatever
`panel.json` says. `<home>/panel.json` (mode `0600`, written atomically) appears only after
the panel listens, and is removed on a clean exit and by `status` when the lock is free.
A background panel's output goes to `<home>/logs/panel.log` (`0600` in a `0700`
directory); the log contains the launch URL with the token. These three are all the panel
writes in the home: a missing home directory is created alone (`0700`, parents as needed),
`logs/` only if missing, and nothing else (no `clones/`, `workspaces/`). The home path must
be absolute, without `..` and without a symlink in it or its parents; an existing `logs`
must be a real directory and `panel.log` must not be a symlink. Otherwise `home_invalid`.
`status` and `stop` apply the same path rule but never create the home (at most the panel's own `panel.lock`): a missing home is `not_running`,
and a symlinked `panel.json` or `panel.lock` is never followed.

## Forms

### `lab-panel-runtime/1`

Printed by `lab panel` (foreground on ready, and `--background`) and stored in `panel.json`.
The key set is exact.

```json
{"schema": "lab-panel-runtime/1", "pid": 4242, "port": 51234,
 "url": "http://p3f9c0a1b2d4e5f60.localhost:51234/?token=<token>",
 "started_at": "2026-09-29T12:00:00.000000+00:00", "home": "/home/you/lab-home"}
```

### `lab-panel-status/1`

`lab panel status`. `state` is `running` or `not_running`; `panel` is a
`lab-panel-runtime/1` object, or `null` when not running or when the lock is held but
`panel.json` is missing or unreadable.

```json
{"schema": "lab-panel-status/1", "state": "running", "panel": {"schema": "lab-panel-runtime/1", "...": "..."}}
```

### `lab-panel-stop/1`

`lab panel stop`. `state` is `stopped` (with the stopped `pid`) or `not_running`
(`pid: null`). `stop` signals a panel only after it proves its identity: it must answer
`/api/health` with the `started_at` recorded in `panel.json` (`stop` connects to
`127.0.0.1:<port>` and sends the `Host` taken from the record's `url`).

```json
{"schema": "lab-panel-stop/1", "state": "stopped", "pid": 4242}
```

### `lab-panel-health/1`

`GET /api/health` on the panel (needs the cookie). Answers 200 even when the service is down.

```json
{"schema": "lab-panel-health/1", "started_at": "…", "home": "/home/you/lab-home",
 "service": {"state": "ok"}}
```

When the service does not answer 200 within 2 seconds:
`"service": {"state": "service_unavailable", "hint": "lab serve --home <H>"}`.

## Panel errors on the wire

Every panel-generated error uses one envelope, with `stage: "panel"`:

```json
{"error": {"code": "forbidden", "stage": "panel", "message": "Open the panel with the link printed by lab panel."}}
```

The closed list (`app.ERRORS`):

| Code | HTTP | Message |
|---|---|---|
| `misdirected_request` | 421 | The panel answers only on its own launch host name and port. |
| `method_not_allowed` | 405 | The panel is read-only. |
| `forbidden` | 403 | Open the panel with the link printed by lab panel. |
| `not_found` | 404 | The panel does not serve this path. |
| `service_unavailable` | 503 | The Lab service is not reachable. (Adds `"hint": "lab serve --home <H>"`.) |

Errors returned by the service on a forwarded route (its own envelopes and statuses) are
passed through unchanged.

### Order of checks

1. **Host** — `Host` must be exactly `<launch host>:<port>`, the host and port of the
   runtime record's `url`; anything else, `127.0.0.1:<port>` and `localhost:<port>`
   included, is 421.
2. **Method** — `GET` or `HEAD`, else 405.
3. **Token** — `GET /?token=<token>` with the right token answers `303` to `/` and sets the
   cookie; a wrong token is 403. Every other path needs the cookie, else 403. Tokens are
   compared in constant time.
4. **Route** — `/api/health`, `/`, `/static/…`, or an allowlisted `/v1/…` path; anything
   else is 404.

`HEAD` passes the panel's method check, but a forwarded `/v1` route answers with the
service's own 405, because the service's `GET` routes do not answer `HEAD`.

Cookie: `lab_panel_<port>=<token>; HttpOnly; SameSite=Strict; Path=/`, host-only (no
`Domain` attribute). The name is port-scoped so panels of several homes do not overwrite
each other. The host name is random per launch (`p` + 16 hex digits + `.localhost`)
because browsers do not isolate cookies by port: on `127.0.0.1` the cookie, and with it
the token, would reach any other HTTP server on loopback (ADR-0018 p. 3, amended).

### Headers on every response

`Cache-Control: no-store`, `Content-Security-Policy: default-src 'self'`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.

## What is forwarded

A request that passes the checks and whose raw path matches a template in `ALLOWED` is sent
to the service over its Unix socket. Matching is on the raw path: a `{…}` segment is one
identifier (`[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`); only the measurement-id segment may
also carry an epoch suffix, `@e<k>` or its single percent-encoded form `%40e<k>` (`k` a
positive integer without leading zeros). The sample route’s `case_id` / `sample_id` segments
also admit core grammar characters and `%XX` UTF-8 bytes; encoded slashes (`%2F`), `.`/`..`
and empty segments never match. Attempt/reflection segments follow `r<NN>-a<MM>` / `r<NN>-x<MM>`.
The service decodes core ids once and validates them; clients must encode once. Other segments
reject `%`-escapes. The query `disclose=private` is forwarded for explicit content disclosure.
**Only the method, the raw path, the raw query and the `Accept` header are sent to the service**; cookies, `Host`, `Last-Event-ID` and everything else are
dropped. Of the service's response, the status, the body (streamed, so SSE works) and
`Content-Type` come back, plus the security headers above. A browser disconnect closes
the stream to the service.

Any transport failure before the response headers — the socket missing, refused, a stale
connection after a service restart, a timeout (5 s to connect; 60 s to read, none for
`…/events`) — is answered `503 service_unavailable` with the hint.

### `ALLOWED` (verbatim from `src/evolution_lab/panel/allowlist.py`)

```python
ALLOWED = (
    "/v1/health",
    "/v1/projects", "/v1/projects/{project_id}", "/v1/projects/{project_id}/operations",
    "/v1/projects/{project_id}/facts", "/v1/projects/{project_id}/status",
    "/v1/projects/{project_id}/confirmations",
    "/v1/scenarios", "/v1/scenarios/{scenario_id}", "/v1/scenarios/{scenario_id}/schemas/{name}/{version}",
    "/v1/guides/requirements",
    "/v1/targets", "/v1/targets/{target_id}",
    "/v1/operations", "/v1/operations/{operation_id}", "/v1/operations/{operation_id}/events",
    "/v1/operations/{operation_id}/report", "/v1/operations/{operation_id}/campaign",
    "/v1/operations/{operation_id}/measurements",
    "/v1/operations/{operation_id}/measurements/{measurement_id}",
    "/v1/operations/{operation_id}/measurements/{measurement_id}/cases/{case_id:path}/samples/{sample_id:path}",
    "/v1/operations/{operation_id}/attempts/{attempt_id}",
    "/v1/operations/{operation_id}/reflections/{reflection_id}",
    "/v1/operations/{operation_id}/diff",
)
```

### `EXCLUDED` (verbatim)

```python
EXCLUDED = (
    "/v1/bundles/{bundle_digest}",
    "/v1/prepared-executions/{prepared_digest}",
    "/v1/tasks/{task_id}",
    "/v1/operations/{operation_id}/exports/{export_digest}/content",  # results archive (spec §2.8)
)
```

**Rule:** a test pins that every `GET` route of the service is in exactly one of the two
lists. A new read route in the service is therefore a conscious decision: add it to
`ALLOWED` or `EXCLUDED` in the same change.

## CLI errors

CLI failures print `{"error": {"code", "stage": "panel", "message"}, …}` to stderr and exit
1. The closed list (`runtime.MESSAGES`); extra top-level fields are listed after.

| Code | Message | Extra fields |
|---|---|---|
| `panel_already_running` | A panel is already running for this Lab home. | `url` (or `null`) |
| `panel_dependencies_missing` | The panel requires the optional panel dependencies (pip install 'evolution-lab[panel]'). Foreground only: with `--background` the child fails and the parent reports `panel_start_failed`; details are in `<home>/logs/panel.log`. | |
| `panel_port_in_use` | The requested panel port is already in use. | |
| `panel_start_failed` | The background panel exited before it became ready. With `--background` every child start error (missing extra, port in use, ...) is reported by the parent as this code; details are in `<home>/logs/panel.log`. | `log` |
| `panel_start_timeout` | The background panel did not become ready in time; the child is terminated (killed after 5 s). | `pid`, `log` |
| `panel_identity_unconfirmed` | The process named in panel.json did not confirm it is this panel; nothing was stopped. | `pid` |
| `panel_stop_timeout` | The panel did not stop in time. | `pid` |
| `home_invalid` | The Lab home path is invalid. | |
