# agent-tokens-dashboard

Static, local token-usage dashboard for the **minimax agent on Windows**. Reads
`runtime-state.sqlite` directly, renders a self-contained `dashboard.html` —
no backend, no external assets, no live connection.

Built for offline visibility: opens via `file://`, refreshes on a Windows
Task Scheduler trigger.

---

## What it does

- Shows tokens used in the current calendar hour (Europe/Moscow)
- Shows tokens used since midnight (running total for the current MSK date)
- Shows tokens used in the **active 5-hour slot** (see "Active window" below)
  with per-hour breakdown
- Shows a **Today · 24H Stream** breakdown: 24 hourly bars (00..23) for the
  current MSK date, coloured by the GitHub contribution palette (4 intensity
  levels by quartile, plus a bright peak highlight), with dashed placeholders
  for future hours
- Shows a 4-week grouped-bar comparison (Mon–Sun per week, oldest → current)
- Marks future days and days with no logged rows as `disabled` (dashed), not as zero
- Shows a **weekly cap threshold** on the current day: a red dashed line with
  a "порог N.NNM" label marking today's spend ceiling, so the weekly cap
  (default 75M tokens) holds across the remaining days of the week. If you
  blow past the line, the level auto-recalculates for the next day (formula
  re-evaluates with new `today_spent` / `days_left`).

**Metric:** `input_tokens + output_tokens`. `cache_read_tokens`,
`cache_write_tokens`, `reasoning_tokens`, and `cost_usd` are intentionally
excluded — they are not the "what got billed to the model" number.

### Today · 24H Stream

Hourly breakdown of the current MSK date, rendered as 24 bars (00..23).
Designed for at-a-glance pattern recognition: when did the burn spike, is
the current hour above/below the day's average, when does the agent go quiet.

Visual model (borrowed from the GitHub contribution heatmap):

| Bar state  | Look                                                              |
| ---------- | ----------------------------------------------------------------- |
| `active`   | Green by intensity quartile (L1 lightest → L4 darkest)            |
| `peak`     | Bright `#00d97e` with a soft glow; the single top-1 hour          |
| `current`  | Same intensity color, thin white outline (hour still accumulating) |
| `empty`    | 2px neutral floor (hour passed, no logged rows)                    |
| `future`   | Dashed placeholder, 55% opacity (hour hasn't started yet)         |

The legend under the title uses the GitHub format: `Less [L1][L2][L3][L4] More`
— only the intensity scale, since `empty` / `future` / `peak` / `current` are
read directly off the bar styles. The meta line in the card head shows today's
total and the peak hour/value (a `Пик: 13:00 (1.35M)` line is the unambiguous
"where we are in the day" anchor). The card is purely a visualisation — no
log/linear toggle, since "consumption by hour" is not an accumulating metric.

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

Constants (timezone offset, slot table, week count, **weekly cap**)
live at the top of `build_dashboard.py` as module-level constants.

---

## Manual rebuild

For an on-demand rebuild (debugging, after schema change, before/after a
heavy workload), double-click `refresh-dashboard.cmd` in Explorer — it
`cd`s to its own directory, runs `python build_dashboard.py`, prints the
build log, and pauses only on a non-zero exit so errors stay inspectable.
Pinnable to the taskbar or Start menu.

Equivalent manual command (anything `cmd` does under the hood):

```powershell
cd "C:\Projects\Python\0803_agent-tokens-dashboard"
python build_dashboard.py
```

---

## Auto-refresh (Windows Task Scheduler)

The dashboard is a static file — schedule `build_dashboard.py` to refresh it.

```powershell
# One-shot, every 5 minutes, as the current user
$action = New-ScheduledTaskAction `
    -Execute "python.exe" `
    -Argument "C:\Projects\Python\0803_agent-tokens-dashboard\build_dashboard.py --quiet"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "agent-tokens-dashboard-refresh" `
    -Action $action -Trigger $trigger -Description "Rebuild dashboard.html every 5 min"
```

**Two-tier refresh.** The 5-minute scheduler refreshes **data** (SQLite →
HTML). In between rebuilds, the browser auto-refreshes the open tab every
**60 seconds** via `<meta http-equiv="refresh" content="60">` in the
generated HTML, so the last build is always on screen without manual F5
and without a persistent process in memory.

---

## Time-zone and metric rules

- **Time zone:** `Europe/Moscow` (UTC+3 year-round, hardcoded constant in
  the script — no DST since 2014)
- **Calendar hours only:** every aggregate bucket is `HH:00:00`–`HH:59:59`
- **Today card:** running total since `00:00` MSK up to and including
  the current in-progress hour; recomputed on every rebuild
- **Active window:** 5-hour slot, selected by current MSK hour. 4 day
  slots (`03:00–08:00`, `08:00–13:00`, `13:00–18:00`, `18:00–23:00`) + 1
  night slot (`23:00–03:00`, 4 hours crossing midnight, half-open). See
  PRD §6.3 for the slot table.
- **Weekly chart:** last 4 ISO weeks, oldest on the left, current on the
  right. Future days of the current week render as `disabled` (dashed
  placeholder), not as zero

---

## Project layout

```
build_dashboard.py               # the only script — open/read/aggregate/render
tests/                           # unit tests, no pytest — run each file directly
  test_windows.py                # 4 — slot boundaries (day/night, midnight wrap)
  test_log_scale.py              # 11 — linear/log, week-total, localStorage script
  test_weekly_cap.py             # 15 — compute_weekly_threshold + render
  test_24h_stream.py             # 15 — compute_today_24h + intensity quartiles + state machine
concepts/                        # design exploration v2 — 4 visual directions
                                 # (concept-ops chosen for production; others retained)
demo_24h.py                      # synthetic-data renderer for the 24H card;
                                 # outputs tmp/dashboard_demo.html with a fake
                                 # "yesterday 14:00" dataset for visual review
refresh-dashboard.cmd            # one-click Windows rebuild helper (Explorer / taskbar)
prd-token-dashboard-prototype.md # spec (PRD)
dashboard.html                   # generated output (gitignored, regenerated on run)
tmp/                             # demo artefacts (gitignored)
runtime-state.sqlite             # the agent's local DB (gitignored, 446MB on this box)
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
