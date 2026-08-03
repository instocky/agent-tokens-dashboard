# agent-tokens-dashboard

Static, local token-usage dashboard for the **minimax agent on Windows**. Reads
`runtime-state.sqlite` directly, renders a self-contained `dashboard.html` —
no backend, no external assets, no live connection.

Built for offline visibility: opens via `file://`, refreshes on a Windows
Task Scheduler trigger.

---

## What it does

- Shows tokens used in the current calendar hour (Europe/Moscow)
- Shows tokens used in the **active 5-hour slot** (see "Active window" below)
  with per-hour breakdown
- Shows a 4-week grouped-bar comparison (Mon–Sun per week, oldest → current)
- Marks future days and days with no logged rows as `disabled` (dashed), not as zero

**Metric:** `input_tokens + output_tokens`. `cache_read_tokens`,
`cache_write_tokens`, `reasoning_tokens`, and `cost_usd` are intentionally
excluded — they are not the "what got billed to the model" number.

---

## Quick start

```powershell
# Default: reads .\runtime-state.sqlite, writes .\dashboard.html
python build_dashboard.py

# Custom paths
python build_dashboard.py --db C:\path\to\runtime-state.sqlite --out dashboard.html

# Dry run (print to stdout, don't write file)
python build_dashboard.py --no-write
```

Then open `dashboard.html` in any browser. No server, no internet.

Requires **Python 3.9+**, stdlib only (no `pip install`).

---

## How it works

1. Open `runtime-state.sqlite` in **read-only** mode via SQLite URI
   (`file:...?mode=ro`) — never blocks the agent's writer
2. Aggregate `input + output` tokens by **MSK date + hour** in SQL
   (`date(ts/1000, 'unixepoch', '+3 hours')`)
3. Compute three views: current hour, active 5h slot, last 4 ISO weeks
4. Render a self-contained `dashboard.html` with inline CSS and an inline
   SVG grouped-bar chart — no Tailwind, no CDN, no external JSON
5. Overwrite `dashboard.html` atomically

The build is short-lived (open → read → close) and idempotent. Safe to run
on a 5-minute schedule.

---

## Data source

Single table: **`local_runtime_token_usage`** in `runtime-state.sqlite`.

| Column              | Used? | Notes                                 |
| ------------------- | ----- | ------------------------------------- |
| `ts`                | yes   | Epoch **milliseconds**; bucketed to MSK |
| `input_tokens`      | yes   | part of main metric                   |
| `output_tokens`     | yes   | part of main metric                   |
| `cache_read_tokens` | no    | excluded by design                    |
| `cache_write_tokens`| no    | excluded by design                    |
| `reasoning_tokens`  | no    | excluded by design                    |
| `cost_usd`          | no    | excluded by design                    |
| `agent_name`        | no    | reserved for future filter            |
| `model`             | no    | reserved for future filter            |
| `session_id`        | no    | —                                     |

If your `ts` is in **seconds** instead of milliseconds, the SQL still works
because every value would land in the same hour bucket shifted by 1000× —
but verify by running `SELECT ts FROM local_runtime_token_usage LIMIT 1;`
first; the script assumes milliseconds.

---

## Configuration

| Flag          | Default                              | Purpose                          |
| ------------- | ------------------------------------ | -------------------------------- |
| `--db PATH`   | `<script_dir>\runtime-state.sqlite`  | Source SQLite file               |
| `--out PATH`  | `<script_dir>\dashboard.html`        | Output HTML                      |
| `--no-write`  | `false`                              | Print to stdout instead of write |
| `--quiet`     | `false`                              | Suppress build log               |

Constants (timezone offset, slot table, week count, palette) live at the
top of `build_dashboard.py` as module-level constants.

---

## Auto-refresh (Windows Task Scheduler)

The dashboard is a static file — schedule `build_dashboard.py` to refresh it.

```powershell
# One-shot, every 5 minutes, as the current user
$action = New-ScheduledTaskAction `
    -Execute "python.exe" `
    -Argument "C:\Projects\Python\0803_sqlite-script\build_dashboard.py --quiet"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "agent-tokens-dashboard-refresh" `
    -Action $action -Trigger $trigger -Description "Rebuild dashboard.html every 5 min"
```

---

## Time-zone and metric rules

- **Time zone:** `Europe/Moscow` (UTC+3 year-round, hardcoded constant in
  the script — no DST since 2014)
- **Calendar hours only:** every aggregate bucket is `HH:00:00`–`HH:59:59`
- **Active window:** 5-hour slot, selected by current MSK hour. 4 day
  slots (`03–07`, `08–12`, `13–17`, `18–22`) + 1 night slot (`23–02`,
  4 hours crossing midnight). See PRD §6.3 for the slot table.
- **Weekly chart:** last 4 ISO weeks, oldest on the left, current on the
  right. Future days of the current week render as `disabled` (dashed
  placeholder), not as zero

---

## Project layout

```
build_dashboard.py   # the only script — open/read/aggregate/render
dashboard.html       # generated output (gitignored, regenerated on run)
runtime-state.sqlite # the agent's local DB (gitignored, 446MB on this box)
```

The prototype HTML (`dashboard-chart-prototype.html`) and the reference
PowerShell scripts (`Explore-SQLiteSchema*.ps1`, `Token-Usage-*.ps1`) are
pre-existing in the workspace but **not part of this project** — they are
left untouched for reference.

---

## Out of scope (intentionally)

- Real-time updates (websocket, SSE, JS polling)
- Multi-user access, auth, hosted server
- Cost analysis as a primary metric
- Filters by `model` / `agent_name` (data is in the table; deferred)
- Detail drill-down screens (PRD open question)
- Linux / macOS support — Windows-first because the source DB is locked
  to the agent's Windows install path
