---
status: accepted
last_updated: 2026-08-21
version: 0.1
---

# Architecture — agentdash-service

> New here? Start with [`README.md`](../README.md) — what the service
> does, how to run it, project layout, time-zone rules. This file is the
> architecture deep-dive: layers, request lifecycle, deployment
> considerations, explicit out-of-scope.

## 1. Purpose

Read-only HTTP API that serves JSON snapshots of token-usage data for
three local HTML dashboards. Reads from a single SQLite file written
by the **minimax** agent runtime. One user, one machine, plain HTTP,
no auth.

Built to replace the legacy `build_dashboard.py` approach (archived in
`tmp/legacy/`) with a live service so dashboards can refresh without
re-running a build script on a 5-minute Windows Task Scheduler loop.

## 2. Non-goals (what this service is NOT)

- **Not a write path.** The DB is opened `mode=ro` at the OS level.
  This service cannot ingest, dedup, or mutate.
- **Not multi-tenant / multi-agent.** Hardcoded to one SQLite file,
  one `AGENTDASH_TIMEZONE`, one user.
- **Not public.** Binds `127.0.0.1` only. CORS is wide-open because
  the dashboards are opened from `file://` on the same machine — see §10.
- **Not real-time.** No WebSocket / SSE / push. Dashboards poll on
  their own 5-minute cadence.
- **Not horizontally scalable.** Single process, no workers, no Docker,
  no reverse proxy assumptions.

## 3. Runtime

| Aspect    | Value                                         |
| --------- | --------------------------------------------- |
| Bind host | `127.0.0.1` (loopback only)                   |
| Port      | `8021`                                        |
| Protocol  | HTTP (plain, no TLS)                          |
| Process   | `uvicorn agentdash_service.main:app`          |
| Python    | `>=3.11` (uv-managed `.venv`)                 |
| Lifecycle | AtLogon (Phase 7 — not yet wired)             |

## 4. Configuration

All env-driven config lives in `src/agentdash_service/config.py` and
uses the `AGENTDASH_` prefix via pydantic-settings. Loaded once at
module import from `.env` (if present) and process environment.

Full table in [`API_CONTRACT.md`](./API_CONTRACT.md#env-vars). Summary:

| Env var | Default | Purpose |
| ------- | ------- | ------- |
| `AGENTDASH_HOST` | `127.0.0.1` | bind host |
| `AGENTDASH_PORT` | `8021` | bind port |
| `AGENTDASH_LOG_LEVEL` | `INFO` | `logging` level |
| `AGENTDASH_DB_PATH` | `C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite` | read-only source |
| `AGENTDASH_TIMEZONE` | `Europe/Moscow` | IANA TZ for all aggregates |
| `AGENTDASH_DEFAULT_MODEL` | `minimax-m3` | model name in cost metadata |
| `AGENTDASH_COST_INPUT_PER_1M_USD` | `0.23` | USD per 1M input tokens |
| `AGENTDASH_COST_OUTPUT_PER_1M_USD` | `0.96` | USD per 1M output tokens |
| `AGENTDASH_WEEKLY_CAP_TOKENS` | `60_000_000` | weekly quota |
| `AGENTDASH_WEEK_COUNT` | `4` | finished ISO weeks in window |

Copy `.env.example` to `.env` to override. **`.env` is gitignored.**

The `TZ: ZoneInfo` constant exported from `config.py` is the single
source of truth for time arithmetic. Don't re-parse TZ strings
elsewhere.

## 5. Code layout

```
src/agentdash_service/
├── main.py              # FastAPI app, CORS, health/ready, router wiring
├── config.py            # Settings (pydantic-settings) + TZ
├── db.py                # read-only SQLite helper (per-request connection)
├── __init__.py          # __version__ = "0.1.0"
├── api/v1/
│   ├── tokens.py        # GET /api/v1/tokens/snapshot
│   ├── projects.py      # GET /api/v1/projects/snapshot
│   │                     # GET /api/v1/projects/{slug}/detail
│   └── sessions.py      # GET /api/v1/sessions/snapshot
├── services/
│   ├── time.py          # now_in_tz, week window, WINDOWS table, days_left
│   ├── cost.py          # compute_cost(input, output, settings) -> float
│   ├── tokens.py        # build_snapshot — main aggregation
│   ├── projects.py      # project-level aggregations
│   ├── sessions.py      # session-level aggregations
│   └── project.py       # per-project detail
└── models/
    ├── tokens.py        # TokensSnapshot, TodayBlock, WeeklyBlock, ...
    ├── projects.py
    └── sessions.py
```

Three layers, strictly one-way:

```
api/v1/*  ──HTTP──▶  services/*  ──SQL──▶  runtime-state.sqlite
   │                       │
   └──Pydantic──▶  models/* (response shape)
```

- **`api/v1/`** — thin: parse path/query, inject `DbConnection`, call one
  service function, return Pydantic model. No business logic. 404s on
  missing detail are an `api/v1/` concern (e.g.
  `projects.py::project_detail` returns 404 when the slug has no
  sessions in the window).
- **`services/`** — pure functions that take a `sqlite3.Connection` and
  return raw dicts/lists. They own SQL, aggregation, window math.
- **`models/`** — Pydantic v2 schemas for response shape. Owned by
  `api/v1/`; services are free of Pydantic imports.

## 6. Request lifecycle (per endpoint)

```
GET /api/v1/tokens/snapshot
  │
  ▼ FastAPI
  router (api/v1/tokens.py)
  │   └── injects DbConnection (db.py:db_session)
  │       └── opens file:{db_path}?mode=ro (sqlite3 OS-level RO)
  │       └── row_factory = sqlite3.Row
  │
  ▼ service
  services/tokens.py::build_snapshot(con, now_in_tz())
  │   └── now_in_tz() — datetime.now(TZ) from config
  │   └── SQL: today totals, hourly buckets, windows, peak hour
  │   └── SQL: weekly aggregates over rolling `week_count` finished weeks
  │   └── SQL: now_session (active session by record_json.status == "started")
  │   └── compute_cost() per aggregate level
  │
  ▼ model
  TokensSnapshot(...) validated and serialized
  │
  ▼
  200 OK application/json
```

Per request: **one** SQLite connection, opened at the start, closed at
the end. No pool, no cache. At ~3 req/min this is fine; if the load
profile changes, swap `db.py::_connect` for a pooled backend without
touching the services layer.

## 7. Endpoints

| Method | Path | Source in legacy | Used by |
| ------ | ---- | ---------------- | ------- |
| GET | `/api/v1/health` | new | liveness probe (always 200) |
| GET | `/api/v1/ready`  | new | readiness probe (pings DB; 503 if not) |
| GET | `/api/v1/tokens/snapshot`    | `build_dashboard.py`         | `dashboard.html` |
| GET | `/api/v1/projects/snapshot`  | `build_project_dashboard.py` | `project-dashboard.html` |
| GET | `/api/v1/projects/{slug}/detail`  | new                              | `project-dashboard.html` row expand |
| GET | `/api/v1/sessions/snapshot`  | `build_session_dashboard.py` | `session-dashboard.html` |

Request and response shapes are in
[`API_CONTRACT.md`](./API_CONTRACT.md#endpoints-in-detail). Keep them in
sync when adding fields.

## 8. Data source

`runtime-state.sqlite` opened in **read-only URI mode**:

```python
uri = f"file:{settings.db_path}?mode=ro"
con = sqlite3.connect(uri, uri=True)
```

Read-only is enforced at the OS level — even a bug in the service
cannot write to the agent's DB.

Three tables, all in the same file. See `API_CONTRACT.md §DB schema`
for column types.

| Table | Role |
| ----- | ---- |
| `local_runtime_token_usage` | per-row `(ts_ms, input, output, session_id)` |
| `local_runtime_message_rows`| per-message rows (used for request counts) |
| `local_runtime_sessions`   | session metadata (`record_json` carries `workspaceDir`, `title`, `status`) |

Timestamps in the DB are **Unix epoch milliseconds**. The service
converts to/from ISO 8601 with offset at the API boundary.

## 9. Time handling

- All aggregates computed in `AGENTDASH_TIMEZONE` (default `Europe/Moscow`).
- `services/time.py::now_in_tz()` is the only place that reads the
  wall clock for business logic. Tests inject a fixed `datetime`.
- `current_week_window(today, week_count)` returns the rolling window
  start (Monday of `week_count` weeks ago) and the current Monday.
- `days_left_in_week(today)` is `8 - isoweekday(today)` — Mon=7, Sun=1.
- 5-hour windows for the today card are defined as a literal table in
  `services/time.py::WINDOWS`. The `night` window wraps midnight
  (`23, 0, 1, 2`) and the service accounts for that in SQL.

Do **not** store `TZ` arithmetic in SQL — SQLite has no native named-TZ
support. Convert at the Python boundary and pass Unix-ms literals to
SQL filters.

## 10. CORS

```python
allow_origins=["*"]
allow_methods=["GET"]
allow_headers=["*"]
```

Wide open because the dashboards open from `file://` and fetch
`http://127.0.0.1:8021`. Browsers treat `file://` as an opaque origin,
so the only practical CORS value is `*`. Single-user, loopback-only —
this is not a security boundary.

If you ever bind the service to a non-loopback address, this CORS
policy becomes a real risk. The default `host=127.0.0.1` is part of
the security model, not just a convenience.

## 11. Logging

- Configured at module import in `main.py` via `logging.basicConfig`,
  level from `settings.log_level`.
- Format: `%(asctime)s %(levelname)s %(name)s %(message)s`.
- Readiness failures log with `exc_info=True` (warning, not error —
  expected on cold start when the DB is briefly unavailable).
- No request-log middleware. If you need per-request visibility, add
  one — keep it out of the hot path otherwise.

## 12. Local development

```powershell
cd C:\Projects\Python\0803_agent-tokens-dashboard
uv run uvicorn agentdash_service.main:app --host 127.0.0.1 --port 8021
```

Smoke check:

```powershell
curl http://127.0.0.1:8021/api/v1/health
curl http://127.0.0.1:8021/api/v1/ready
curl http://127.0.0.1:8021/api/v1/tokens/snapshot | Select-Object -First 1
```

Tests:

```powershell
uv run pytest -q
```

`uv run` is mandatory — without it PowerShell resolves `pytest` to
the system Python and you get `ModuleNotFoundError` on the project's
own imports.

## 13. Deployment

Manual today. Phase 7 (pending) wires an **AtLogon** Windows Task
Scheduler entry that runs:

```powershell
uv run uvicorn agentdash_service.main:app --host 127.0.0.1 --port 8021
```

in the project working directory. Until that's registered, start the
service by hand after each reboot.

The service is **not** designed for `nssm` / `sc.exe` / NSSM-style
service hosting — uvicorn is foreground, and Task Scheduler handles
"run on logon" without the SCM overhead.

## 14. What this service does NOT do

Out of scope, do not add without an explicit LLD:

- Write paths, ingest, dedup, schema migrations
- Auth, sessions, multi-user
- WebSocket / SSE / push / live tail
- Multi-tenant / multi-agent
- Connection pooling, caching, Redis, message brokers
- TLS, reverse proxy, Docker, multi-worker
- CI / CD pipelines, pre-commit, automated releases
- Cost model beyond the two configurable per-1M prices
- Filtering by `model` / `agent_name` (data is in the table; deferred)
- A "detail" view beyond the per-project endpoint
- Linux / macOS support — the source DB is locked to the Windows
  install path of the agent
