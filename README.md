# agentdash-service

Local FastAPI service that serves JSON snapshots for token-usage dashboards,
plus three self-contained HTML dashboards in the project root. Reads
`runtime-state.sqlite` directly, exposes a read-only HTTP API on
`127.0.0.1:8021`.

Built for the **minimax** agent on Windows. One user, one machine, plain HTTP,
no auth.

Open any of the three HTML files directly in a browser (`file://`) — it
fetches the JSON over loopback. The service auto-starts on logon via a
Windows Task Scheduler entry (see "Auto-start" below).

> Looking for architecture deep-dive, request lifecycle, and module layout?
> See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).
>
> Looking for endpoint request/response shapes, error codes, DB schema,
> env var table? See [`docs/API_CONTRACT.md`](./docs/API_CONTRACT.md).

---

## What it does

- **Three dashboards, three endpoints, one source of truth:**
  - `dashboard.html` — single-page view: today totals, active 5h window
    with per-hour breakdown, 4-week comparison chart, Today · 24H Stream,
    weekly cap threshold, current session card
  - `project-dashboard.html` — per-project totals (tokens, cost, sessions,
    duration), sortable, expandable rows; on expand shows a 5-week day
    grid and a 24H heatmap for the selected day
  - `session-dashboard.html` — per-session totals (tokens, cost, requests,
    duration), sortable
- **All three auto-refresh every 5 minutes** in the browser via
  `setInterval`, pulling a fresh JSON snapshot from the API
- All metrics computed in `Europe/Moscow` (configurable via
  `AGENTDASH_TIMEZONE`)
- **Read-only at the OS level** — the service opens `runtime-state.sqlite`
  with SQLite URI `mode=ro`, so even a bug in the service cannot write
  to the agent's DB

**Metric:** `input_tokens + output_tokens`. `cache_read_tokens`,
`cache_write_tokens`, `reasoning_tokens` are excluded from the primary
metric. `cost_usd` is computed by the service from configurable
per-1M-token prices and exposed as metadata on every aggregate.

### Today · 24H Stream

Hourly breakdown of the current MSK date, rendered as 24 bars (00..23).
Designed for at-a-glance pattern recognition: when did the burn spike, is
the current hour above/below the day's average, when does the agent go quiet.

Visual model (borrowed from the GitHub contribution heatmap):

| Bar state  | Look                                                                |
| ---------- | ------------------------------------------------------------------- |
| `active`   | Green by intensity quartile (L1 lightest → L4 darkest)              |
| `peak`     | Bright `#00d97e` with a soft glow; the single top-1 hour            |
| `current`  | Same intensity color, thin white outline (hour still accumulating)  |
| `empty`    | 2px neutral floor (hour passed, no logged rows)                     |
| `future`   | Dashed placeholder, 55% opacity (hour hasn't started yet)           |

The legend under the title uses the GitHub format `Less [L1][L2][L3][L4] More`.
The card is purely a visualisation — no log/linear toggle, since
"consumption by hour" is not an accumulating metric.

### Weekly cap threshold

On the current day of the current week, the chart draws a red dashed line
with a `порог N.NNM` label marking today's spend ceiling. The level is
recomputed on every snapshot so it tracks `today_spent` and `days_left`:
blow past the line today, and the threshold for tomorrow recalculates
automatically. Formula in
[`docs/API_CONTRACT.md`](./docs/API_CONTRACT.md) §"Endpoints in detail".

---

## Quick start

```powershell
cd C:\Projects\Python\0803_agent-tokens-dashboard
uv run uvicorn agentdash_service.main:app --host 127.0.0.1 --port 8021
```

Then open one of:

- `dashboard.html` (overall view)
- `project-dashboard.html` (per-project)
- `session-dashboard.html` (per-session)

in any browser. No server-side rendering, no build step.

**Required:** Python `>=3.11` managed by `uv`. The `uv run` prefix is
mandatory — bare `uvicorn` from PowerShell resolves to the system Python
and you get `ModuleNotFoundError` on the project's own imports.

Smoke check the API directly:

```powershell
curl http://127.0.0.1:8021/api/v1/health
curl http://127.0.0.1:8021/api/v1/ready
```

Run the test suite:

```powershell
uv run pytest -q
```

---

## How it works

1. `uvicorn` loads `agentdash_service.main:app` from `src/`
2. FastAPI registers three routers under `/api/v1/*` plus
   `health`/`ready` probes
3. Browser opens one of the three HTML files in the project root
   (`file://`); the inline JS calls `fetch('http://127.0.0.1:8021/...')`
4. CORS middleware (`allow_origins=["*"]`, `allow_methods=["GET"]`)
   permits the cross-origin request from `file://` to the loopback
   service — single-user, no security boundary
5. Router injects a read-only `sqlite3.Connection` (URI `mode=ro`),
   delegates to a `services/*::build_*` function
6. Service runs SQL aggregations (totals, hour buckets, weekly window,
   active session) and computes `cost_usd` from configurable per-1M
   prices
7. Result is validated against a Pydantic v2 model and serialised to
   JSON; browser JS renders the DOM
8. Browser's `setInterval` re-fetches every 5 minutes; the loop is
   closed without any process in memory outside the uvicorn worker

The service holds **one** SQLite connection per request (open → read →
close). At the current ~3 req/min workload this is fine; the swap to a
pooled backend is a one-file change in `db.py`.

---

## Data source

`runtime-state.sqlite` — written by the **minimax** agent runtime,
opened read-only by the service.

Three tables, all in the same file. Full schema in
[`docs/API_CONTRACT.md`](./docs/API_CONTRACT.md) §"DB schema".

| Table                              | Used for                                  |
| ---------------------------------- | ----------------------------------------- |
| `local_runtime_token_usage`        | every `total` / `input` / `output` figure |
| `local_runtime_message_rows`       | `user_messages`, `requests` per session   |
| `local_runtime_sessions`           | `workspace_dir`, `title`, `status` (active session detection), `project` slug |

Timestamps in the DB are **Unix epoch milliseconds**. The service
converts to/from ISO 8601 with offset at the API boundary. If your `ts`
is in **seconds** instead of milliseconds, all aggregates shift by a
1000× factor and become wrong — verify with
`SELECT ts FROM local_runtime_token_usage LIMIT 1;` first.

The file is expected at
`C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite` (override via
`AGENTDASH_DB_PATH`). It is **not** committed to the repo.

---

## Configuration

All config is env-driven via the `AGENTDASH_` prefix, loaded once at
import by pydantic-settings. Copy `.env.example` to `.env` to override.
`.env` is gitignored. The full table lives in
[`docs/API_CONTRACT.md`](./docs/API_CONTRACT.md) §"Env vars". Summary:

| Env var                          | Default                                            | Purpose                  |
| -------------------------------- | -------------------------------------------------- | ------------------------ |
| `AGENTDASH_HOST`                 | `127.0.0.1`                                        | bind host                |
| `AGENTDASH_PORT`                 | `8021`                                             | bind port                |
| `AGENTDASH_LOG_LEVEL`            | `INFO`                                             | `logging` level          |
| `AGENTDASH_DB_PATH`              | `C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite` | read-only source     |
| `AGENTDASH_TIMEZONE`             | `Europe/Moscow`                                    | IANA TZ for aggregates   |
| `AGENTDASH_DEFAULT_MODEL`        | `minimax-m3`                                       | model name in cost metadata |
| `AGENTDASH_COST_INPUT_PER_1M_USD`| `0.23`                                             | USD per 1M input tokens  |
| `AGENTDASH_COST_OUTPUT_PER_1M_USD`| `0.96`                                            | USD per 1M output tokens |
| `AGENTDASH_WEEKLY_CAP_TOKENS`    | `60_000_000`                                       | weekly quota             |
| `AGENTDASH_WEEK_COUNT`           | `4`                                                | finished ISO weeks shown |

The `TZ: ZoneInfo` constant in `config.py` is the single source of
truth for time arithmetic. Don't re-parse TZ strings elsewhere.

---

## Local development

Restart after code changes (uvicorn is foreground, no `--reload` in the
recommended command):

```powershell
Get-Process uvicorn -ErrorAction SilentlyContinue | Stop-Process -Force
uv run uvicorn agentdash_service.main:app --host 127.0.0.1 --port 8021
```

If `uvicorn` is not running as a process with that exact name (e.g.
started through `python -m uvicorn`), fall back to:

```powershell
Get-NetTCPConnection -LocalPort 8021 -State Listen |
  Select-Object -ExpandProperty OwningProcess |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

Tests:

```powershell
uv run pytest -q
```

Lint:

```powershell
uv run ruff check .
```

If port `8021` is already in use you'll see `WinError 10048` on startup
— kill the offending process first, or pick a different
`AGENTDASH_PORT` and reconfigure the dashboards' `fetch` URL.

---

## Auto-start (Windows Task Scheduler)

Wired to **AtLogOn** via a single scheduled task. Four files in
`scripts/`:

| File | Role |
| ---- | ---- |
| `run-service.vbs`     | hidden launcher — invokes `run-service.cmd` with `WindowStyle=0` (SW_HIDE) so the task does not pop a visible `cmd.exe` window in the interactive session |
| `run-service.cmd`     | thin wrapper, `cd` to project, `uv run uvicorn ... >> log 2>&1` |
| `register-task.ps1`   | idempotent, self-elevates via UAC; registers `wscript.exe` → `run-service.vbs` as the action |
| `unregister-task.ps1` | idempotent rollback, self-elevates via UAC |

The VBS wrapper exists because Task Scheduler launches console
scripts with default visibility. Pointing the action directly at
the .cmd makes a `cmd.exe` window flash on every logon and every
crash-restart. The VBS hides it. See `CHANGELOG.md` 2026-08-22
for the full rationale.

Install:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
# (one UAC prompt, then: registered 'agentdash-service' (AtLogOn -> ...\run-service.vbs))
```

Verify:

```powershell
Get-ScheduledTask -TaskName 'agentdash-service'   # State = Ready
curl http://127.0.0.1:8021/api/v1/health          # {"status":"ok"}  -- preferred
Get-Content "$env:LOCALAPPDATA\agentdash-service\service.log"
```

> `Get-NetTCPConnection -LocalPort 8021 -State Listen` is **not**
> recommended as the primary smoke test — its backing CIM query
> occasionally returns "no objects" for freshly-bound loopback
> sockets. Use `curl` (above) or
> `netstat -ano | Select-String ":8021" | Select-String "LISTENING"`.

Remove:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\unregister-task.ps1
```

`register-task.ps1` refuses to overwrite an existing task and refuses
to register while port 8021 is already bound — that catches a manual
uvicorn left running so we don't silently double-bind. Unregister is a
no-op if the task is gone.

Full task definition (trigger, principal, restart policy, log path) is
in [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) §13.

The two-tier refresh model from the static-build days no longer applies
in full:

- **Data freshness** — the service reads SQLite on every request
  (5-minute browser poll), so no build step is needed
- **UI freshness** — the browser's `setInterval` re-fetches every
  5 minutes; the HTML files don't need `<meta http-equiv="refresh">`

---

## Time-zone and metric rules

- **Time zone:** `Europe/Moscow` (UTC+3 year-round, no DST since 2014).
  Configurable via `AGENTDASH_TIMEZONE`
- **Calendar hours only:** every aggregate bucket is `HH:00:00`–`HH:59:59`
- **Today totals:** running sum since `00:00` MSK up to and including
  the current in-progress hour; recomputed on every snapshot
- **Active window:** 5-hour slot, selected by current MSK hour. 4 day
  slots (`03:00–08:00`, `08:00–13:00`, `13:00–18:00`, `18:00–23:00`) + 1
  night slot (`23:00–03:00`, 4 hours crossing midnight, half-open)
- **Weekly chart:** last `AGENTDASH_WEEK_COUNT` finished ISO weeks,
  oldest on the left, current on the right. Future days of the current
  week render as `disabled` (dashed placeholder), not as zero
- **Weekly cap threshold:** red dashed line on the current day, marking
  `(weekly_cap − today_spent) / days_left` floored to zero. Recomputed
  per snapshot
- **Project slug:** `project_from_workspace(workspace_dir)` strips the
  `YYYY_` prefix and lowercases the last path segment

---

## Project layout

```
agentdash-service/
├── README.md                      # this file
├── pyproject.toml                 # uv-managed, Python >= 3.11
├── uv.lock
├── .env.example                   # copy to .env to override defaults
├── dashboard.html                 # client (file://), fetches /tokens/snapshot
├── project-dashboard.html         # client, fetches /projects/snapshot
│                                  #       + /projects/{slug}/detail on expand
├── session-dashboard.html         # client, fetches /sessions/snapshot
├── src/agentdash_service/         # FastAPI app
│   ├── main.py                    #   app, CORS, health/ready
│   ├── config.py                  #   Settings (pydantic-settings) + TZ
│   ├── db.py                      #   read-only SQLite helper
│   ├── api/v1/                    #   thin routers — no business logic
│   │   ├── tokens.py              #     GET /api/v1/tokens/snapshot
│   │   ├── projects.py            #     GET /api/v1/projects/snapshot
│   │   │                          #     GET /api/v1/projects/{slug}/detail
│   │   └── sessions.py            #     GET /api/v1/sessions/snapshot
│   ├── services/                  #   pure SQL + aggregation
│   │   ├── time.py                #     WINDOWS table, week math, TZ
│   │   ├── cost.py                #     compute_cost(input, output, settings)
│   │   ├── tokens.py              #     build_snapshot — main aggregation
│   │   ├── projects.py            #     per-project aggregates
│   │   ├── sessions.py            #     per-session aggregates
│   │   └── project.py             #     per-project detail (5w grid + 24h)
│   └── models/                    #   Pydantic v2 response schemas
├── tests/                         # pytest, 15 tests
├── scripts/                       # Windows Task Scheduler install/remove
│   ├── run-service.vbs            #   hidden launcher (WScript.Shell, WindowStyle=0)
│   ├── run-service.cmd            #   log-redirecting wrapper
│   ├── register-task.ps1          #   self-elevating, idempotent
│   └── unregister-task.ps1        #   self-elevating, idempotent
├── docs/
│   ├── ARCHITECTURE.md            # architecture deep-dive, layers, lifecycle
│   └── API_CONTRACT.md            # endpoint shapes, DB schema, env vars
├── CHANGELOG.md                   # dated, Keep-a-Changelog format
└── tmp/legacy/                    # pre-service artefacts (Phase 9 cleanup pending)
```

The legacy directory (`tmp/legacy/`) holds the previous static-build
implementation (`build_dashboard.py` and friends) and the prototype PRD.
Kept temporarily for reference; will be removed in Phase 9 once the
service is signed off.

---

## Out of scope (intentionally)

- Write paths, ingest, dedup, schema migrations — DB is read-only
- Auth, multi-user, sessions, hosted server
- WebSocket / SSE / push / live tail — dashboards poll on their own cadence
- Cost as a primary metric — it's metadata on every aggregate
- Filters by `model` / `agent_name` (data is in the table; deferred)
- Connection pooling, caching, Redis, message brokers
- TLS, reverse proxy, Docker, multi-worker
- CI / CD pipelines, pre-commit, automated releases
- Linux / macOS support — the source DB is locked to the Windows
  install path of the agent
