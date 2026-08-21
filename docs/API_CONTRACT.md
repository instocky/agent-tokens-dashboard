---
status: accepted
last_updated: 2026-08-21
version: 0.1
---

# API Contract — agentdash-service v1

## Purpose

Read-only HTTP API for token-usage dashboards. Reads from
`runtime-state.sqlite` (one agent), serves 3 dashboard snapshots
over plain HTTP on `127.0.0.1:8021`. One user, no auth.

## Endpoints

| Method | Path | Purpose | Source in `tmp/legacy/` |
|---|---|---|---|
| GET | `/api/v1/health` | Liveness (always 200) | new |
| GET | `/api/v1/ready` | Readiness (DB ping) | new |
| GET | `/api/v1/tokens/snapshot` | dashboard.html | `build_dashboard.py` |
| GET | `/api/v1/projects/snapshot` | project-dashboard.html | `build_project_dashboard.py` |
| GET | `/api/v1/sessions/snapshot` | session-dashboard.html | `build_session_dashboard.py` |

## Conventions

- **TZ** — `Europe/Moscow` (IANA, configurable via `AGENTDASH_TIMEZONE`)
- **Window** — 4 finished ISO weeks + current = 5 weeks total
- **Today window** — `[00:00 today, now]` in configured TZ
- **Timestamps in DB** — Unix epoch milliseconds
- **Timestamps in API** — ISO 8601 with offset (e.g. `2026-08-21T11:30:00+03:00`)
- **Active session** — `record_json.status == "started"`
- **Project slug** — `project_from_workspace(workspace_dir)` strips `YYYY_` prefix
- **Empty data** — zeros + `null`, never 404
- **Cost** — `cost_usd: float` at every aggregate level; client formats as `$X.XX`

## DB schema (read-only)

Three tables in `runtime-state.sqlite`:

```sql
local_runtime_token_usage (
    session_id     TEXT,
    ts             INTEGER,    -- Unix epoch ms
    input_tokens   INTEGER,
    output_tokens  INTEGER
)

local_runtime_message_rows (
    session_id     TEXT,
    created_at_ms  INTEGER,    -- Unix epoch ms
    role           TEXT        -- 'user' | other
)

local_runtime_sessions (
    session_id     TEXT PRIMARY KEY,
    record_json    TEXT        -- JSON: workspaceDir, title, status
)
```

## Env vars

| Name | Type | Default | Notes |
|---|---|---|---|
| `AGENTDASH_HOST` | str | `127.0.0.1` | bind host |
| `AGENTDASH_PORT` | int | `8021` | bind port |
| `AGENTDASH_LOG_LEVEL` | str | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `AGENTDASH_DB_PATH` | Path | `C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite` | read-only |
| `AGENTDASH_TIMEZONE` | str | `Europe/Moscow` | IANA TZ name |
| `AGENTDASH_DEFAULT_MODEL` | str | `minimax-m3` | model name for cost metadata |
| `AGENTDASH_COST_INPUT_PER_1M_USD` | float | `0.23` | per 1M input tokens |
| `AGENTDASH_COST_OUTPUT_PER_1M_USD` | float | `0.96` | per 1M output tokens |
| `AGENTDASH_WEEKLY_CAP_TOKENS` | int | `60000000` | weekly quota |
| `AGENTDASH_WEEK_COUNT` | int | `4` | finished weeks in window |

## Cost formula

```python
cost_usd = (input_tokens  / 1_000_000) * AGENTDASH_COST_INPUT_PER_1M_USD
         + (output_tokens / 1_000_000) * AGENTDASH_COST_OUTPUT_PER_1M_USD
```

## Error responses

- 200 OK — normal response
- 503 Service Unavailable — `/ready` only, when DB unreachable
- 500 Internal Server Error — unhandled exception (logged, not exposed)

## Endpoints in detail

### `GET /api/v1/health`

Liveness. Always 200 if process is up. No DB check.

**Response 200**:
```json
{"status": "ok"}
```

### `GET /api/v1/ready`

Readiness. Pings DB. 200 if OK, 503 if not.

**Response 200**:
```json
{"status": "ready"}
```

**Response 503**:
```json
{"status": "not-ready", "reason": "<error message>"}
```

### `GET /api/v1/tokens/snapshot`

Full data for `dashboard.html`. One request = one snapshot.

**Response 200**:
```json
{
  "now_msk": "2026-08-21T11:30:00+03:00",
  "today": {
    "date": "2026-08-21",
    "totals": {
      "input": 12345,
      "output": 6789,
      "total": 19134,
      "cost_usd": 0.0093
    },
    "meta": {
      "sessions": 4,
      "user_messages": 12,
      "avg_requests_per_session": 3.0
    },
    "hourly": [
      {
        "hour": 0,
        "input": 0,
        "output": 0,
        "total": 0,
        "cost_usd": 0.0,
        "intensity": "L0",
        "is_current": false,
        "is_future": false,
        "is_empty": true
      }
    ],
    "windows": [
      {
        "name": "morning",
        "label": "03:00–08:00",
        "input": 1000,
        "output": 500,
        "total": 1500,
        "cost_usd": 0.00071,
        "is_current": false
      }
    ],
    "current_window": {"name": "midday", "label": "08:00–13:00"},
    "peak_hour": {"hour": 10, "tokens": 8000, "cost_usd": 0.0039}
  },
  "now_session": {
    "session_id": "abc123",
    "tokens": {"input": 5000, "output": 2000, "total": 7000, "cost_usd": 0.0031},
    "user_requests": 4,
    "path": "C:/Projects/Python/0803_...",
    "project": "0803_...",
    "title": "Add feature X",
    "duration_ms": 1234567
  },
  "weekly": {
    "weeks": [
      {
        "label": "W-29",
        "monday": "2026-07-13",
        "is_current": false,
        "days": [
          {
            "date": "2026-07-13",
            "input": 1000,
            "output": 500,
            "total": 1500,
            "cost_usd": 0.00071
          }
        ]
      }
    ],
    "cap_tokens": 60000000,
    "threshold_tokens": 8500000,
    "weekly_spent_tokens": 12500000,
    "days_left": 4
  },
  "sparklines": {
    "current": [100, 200, 300],
    "today":   [0, 0, 500, 1000],
    "window":  [200, 400, 600]
  }
}
```

`now_session` is `null` when no activity today. `weekly.weeks[].days[]` is
`null` for future days and days with no data. `peak_hour` is `null` if no
activity today.

### `GET /api/v1/projects/snapshot`

Full data for `project-dashboard.html`.

**Response 200**:
```json
{
  "now_msk": "2026-08-21T11:30:00+03:00",
  "window": {"start": "2026-07-13", "end": "2026-08-21"},
  "projects": [
    {
      "project": "0803_agent-tokens-dashboard",
      "last_update": "2026-08-21",
      "max_ms": 1724234567000,
      "duration_ms": 12345678,
      "tokens": 1234567,
      "input_tokens": 800000,
      "output_tokens": 434567,
      "cost_usd": 0.6011,
      "sessions": 4,
      "is_active": true,
      "time_series": {
        "weeks": [
          {
            "label": "W-29",
            "days": [
              {
                "date": "2026-07-13",
                "input": 1000,
                "output": 500,
                "total": 1500,
                "cost_usd": 0.00071
              }
            ]
          }
        ]
      }
    }
  ]
}
```

### `GET /api/v1/sessions/snapshot`

Full data for `session-dashboard.html`.

**Response 200**:
```json
{
  "now_msk": "2026-08-21T11:30:00+03:00",
  "window": {"start": "2026-07-13", "end": "2026-08-21"},
  "sessions": [
    {
      "session_id": "abc123",
      "title": "Add feature X",
      "project": "0803_agent-tokens-dashboard",
      "workspace_dir": "C:/Projects/Python/0803_agent-tokens-dashboard",
      "start_msk": "2026-08-21",
      "end_msk": "2026-08-21",
      "max_ms": 1724234567000,
      "duration_ms": 1234567,
      "tokens": 12345,
      "input_tokens": 8000,
      "output_tokens": 4345,
      "cost_usd": 0.0060,
      "requests": 4,
      "is_active": false
    }
  ]
}
```

## Out of scope

- Write / ingest / dedup
- Auth (single user — `auth-jwt-argon2` not used)
- Push / WebSocket / SSE
- Multi-tenant / multi-agent
- Cache
- Migrations
- Multi-worker / Docker / external integrations
