# `jobs`

Config-driven **scheduled jobs**, run by the long-lived `schwab server`
daemon. Each job is a small JSON file with a cron schedule; the server
reads them, fires each job at its time as an isolated worker process,
tracks status, and reloads on demand. This replaces the old
`com.schwab-cli.scheduler` launchd job (see *Migration* below).

The server's auth-maintenance loop keeps the OAuth refresh token alive,
so jobs always run with a fresh session — jobs never run their own
browser login.

## Job config

One file per job at `~/.config/schwab_cli/jobs/<id>.json`. The filename
stem is the job id; `cron` (evaluated in `timezone`) controls when it
runs — no filename ordering needed.

```jsonc
// ~/.config/schwab_cli/jobs/market-data.json
{
  "schema_version": 1,
  "name": "Market Data (Volatility)",
  "enabled": true,
  "cron": "0 17 * * 1-5",          // 5-field cron
  "timezone": "America/New_York",  // IANA tz
  "type": "command",               // "command" | "python"
  "command": ["dataset", "update", "--group", "volatility", "--skip-wait"],
  "timeout_s": 57600,              // optional (default 16h)
  "retries": 1,                    // optional; retried on auth failure
  "retry_delay_s": 120             // optional
}
```

- **`type: "command"`** runs `schwab <command…>` as an isolated worker.
- **`type: "python"`** imports a dotted `"runner": "module.func"` and
  calls it (`args`/`kwargs` optional). A small denylist blocks obviously
  dangerous modules (`builtins`, `subprocess`, `os`, …).

### Staging vs active (`jobs/.current/`)

`jobs/*.json` is the **editable** copy; `jobs/.current/<id>.json` is the
**validated, running** copy. On server start and on `jobs reload`, valid
configs are atomically promoted to `.current/`. If you edit a job into an
invalid state, the edit is **rejected** and the last-good version keeps
running, flagged `outdated` — a bad edit never takes a working job down.
Runtime state lives in `jobs/.current/state.json` (don't hand-edit
`.current/`).

## Commands

| Command | What it does |
|---------|--------------|
| `schwab jobs init` | Write the default job configs (market-data / accounts / indices) if missing. |
| `schwab jobs list` | List staged configs and any validation errors. |
| `schwab jobs status [--json]` | Per-job status: state, schedule, last run + result, next run. |
| `schwab jobs reload` (alias `sync`) | Re-validate configs and signal the running server to apply them; prints a per-job `old → new` report. |
| `schwab jobs run <id>` | Run a job once in the foreground (does not affect the schedule). |
| `schwab jobs enable <id>` / `disable <id>` | Flip a job's `enabled` flag and signal the server. |
| `schwab jobs migrate` | Cutover: uninstall the legacy `com.schwab-cli.scheduler`, then write default jobs. |

`jobs status` example:

```
market-data:       scheduled, cron "0 17 * * 1-5" America/New_York
                   next run 2026-05-28 17:00 EDT
                   last run 2026-05-27 17:00 EDT (ok)

accounts:          running, cron "0 17 * * 1-5" America/New_York
                   running now (pid 71022)
                   last run 2026-05-27 17:00 EDT (ok)
```

## How scheduling works

The server runs an in-process scheduler thread: it computes each enabled
job's next run, sleeps until the soonest, then spawns `schwab jobs run
<id>` as an isolated worker only at fire time, monitors it to completion
(exit code + captured log = status), and reschedules. A failed run
caused by an auth blip (`exit 2`) triggers a token renewal and one
retry; other failures emit a notification. A job already running is
skipped rather than piled up. `jobs reload` (SIGHUP) re-applies configs
**without** interrupting any in-flight worker.

When `--enable-mcp`/`--enable-rest` is on, `GET /admin/jobs` exposes the
live status on the same port.

## Migration from `dataset cron`

Scheduling moved from the standalone `com.schwab-cli.scheduler` launchd
job into `schwab server`. To cut over:

```bash
schwab jobs migrate     # removes the old scheduler, writes default jobs
schwab server install   # (or restart) so the daemon runs the jobs
schwab jobs status      # confirm
```

`dataset cron install` is **deprecated** — it no longer installs a
scheduler (it points you here). `schwab dataset sync` and `dataset
update …` still work for manual/one-off runs and are exactly what the
default `command` jobs call.

See also: [`server`](server.md).
