"""build_project_dashboard.py — static per-project dashboard for the last 4 ISO weeks.

Читает `local_runtime_token_usage`, `local_runtime_message_rows` и
`local_runtime_sessions` из runtime-state.sqlite, группирует сессии по
проекту (slug из workspaceDir) и собирает self-contained
`project-dashboard.html` (без backend, без внешнего JSON).

Окно: 4 последние завершённых ISO-недели (Пн–Вс) + текущая = 5 недель
всего. Например, для today=2026-08-07 (W-32) окно = [2026-07-06,
2026-08-10) MSK, т.е. W-28..W-32. Реальный SQL-фильтр обрезается по
now_msk в main() — будущих дней в БД нет, так что это эквивалентно
"включена вся текущая неделя до текущего момента".

Default sort: проект с самой свежей активностью (MAX created_at_ms в
окне desc), tie-break по tokens desc. Юзер может пересортировать таблицу
в браузере — клик на `<th>` меняет порядок, выбор помнится в localStorage
(ключ "agent-tokens-dashboard:sort") и переживает 60s meta-refresh.

Meta-workspace'ы (`~/.mavis/...`, `~/.minimax/...`) скрываются — это
служебные workspace'ы агента, не реальные проекты. Сессии с пустым/None
workspaceDir тоже не попадают.

Активные проекты: если хотя бы одна сессия проекта имеет
status='started' в record_json, строка помечается бейджем "active" и
лёгкой акцентной подсветкой.

Запуск:  python build_project_dashboard.py
Опции:   --db <path>     путь к sqlite (по умолчанию DB_PATH ниже)
         --out <path>    путь к выходному HTML (по умолчанию OUTPUT_PATH ниже)
         --no-write      не записывать файл (dry-run, печатает в stdout)
         --quiet         не печатать лог
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---- constants -------------------------------------------------------------

# Абсолютные пути по умолчанию — рядом со скриптом. Переопределяются --db/--out.
DB_PATH: Path = Path("C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite")
OUTPUT_PATH: Path = Path(__file__).resolve().parent / "project-dashboard.html"

# Europe/Moscow = UTC+3 круглый год (с 2014 без перехода на зимнее время).
# Хардкод константой, как и в build_dashboard.py / build_session_dashboard.py —
# без zoneinfo-зависимостей.
MSK = timezone(timedelta(hours=3))

# Размер окна — 4 завершённых ISO-недели + текущая (5 недель всего).
WEEK_COUNT: int = 4

# Префикс даты в имени директории (формат YYYY_), который скрываем, чтобы
# вытащить чистый slug. Пример: "0807_db-contingent" → "db-contingent".
# Захватываем только ведущие 4 цифры + "_" — ровно одну итерацию (count=1),
# чтобы случайный "_" в середине имени не тронуть.
_DATE_PREFIX_RE = re.compile(r"^\d{4}_")

# Meta-workspace'ы агента. Сессии в этих workspace'ах скрываются — это не
# реальные проекты, а служебные директории. Проверяем по компонентам пути,
# чтобы не спутать с проектом, в имени которого случайно есть ".mavis"
# как подстрока.
_META_DIRS: frozenset[str] = frozenset({".mavis", ".minimax"})


# ---- domain types ----------------------------------------------------------

@dataclass(frozen=True)
class WeekSpan:
    """Одна ISO-неделя окна: Пн–Вс включительно (MSK dates)."""
    monday: date
    sunday: date

    @property
    def label(self) -> str:
        return f"W-{self.monday.isocalendar()[1]}"


@dataclass(frozen=True)
class ProjectRow:
    """Одна строка таблицы project dashboard.

    Все даты — MSK. `max_ms` нужен для сортировки "свежие сверху" — это
    MAX(created_at_ms) среди сессий проекта в окне.
    """
    project: str
    last_update: date
    max_ms: int
    duration_ms: int
    tokens: int
    sessions: int
    is_active: bool


# ---- CLI -------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Сгенерировать self-contained project-dashboard.html из runtime-state.sqlite.",
    )
    p.add_argument("--db", type=Path, default=DB_PATH,
                   help=f"Путь к SQLite (default: {DB_PATH})")
    p.add_argument("--out", type=Path, default=OUTPUT_PATH,
                   help=f"Путь к выходному HTML (default: {OUTPUT_PATH})")
    p.add_argument("--no-write", action="store_true",
                   help="Не записывать файл (dry-run, печатает в stdout).")
    p.add_argument("--quiet", action="store_true",
                   help="Не печатать лог сборки.")
    return p.parse_args()


# ---- IO --------------------------------------------------------------------

def open_db(path: Path) -> sqlite3.Connection:
    """Открыть SQLite в режиме read-only через URI.

    mode=ro гарантирует, что мы не сможем случайно писать в базу
    и не заблокируем писателей runtime'а.
    """
    if not path.exists():
        raise FileNotFoundError(f"SQLite не найден: {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


# ---- window ----------------------------------------------------------------

def compute_window(today: date) -> tuple[datetime, datetime, list[WeekSpan]]:
    """4 завершённых ISO-недели (Пн–Вс) + текущая = 5 недель всего.

    Возвращает (start_dt, end_dt, weeks), где:
      - start_dt — MSK midnight понедельника самой ранней недели окна
      - end_dt   — MSK midnight понедельника следующей недели (exclusive,
                   т.е. конец воскресенья текущей недели включительно)
      - weeks    — список WeekSpan, oldest-first (W-28, W-29, ..., W-32)

    Реальный SQL-фильтр в main() обрезается по now_msk, не по end_dt —
    это нужно, чтобы future-dated строки не попали и чтобы "позже вечером"
    та же сборка дала больше сессий за текущий день без правок кода.
    """
    iso = today.isocalendar()  # (iso_year, iso_week, iso_weekday 1..7)
    current_monday = today - timedelta(days=iso[2] - 1)
    earliest_monday = current_monday - timedelta(weeks=WEEK_COUNT)

    weeks: list[WeekSpan] = []
    for i in range(WEEK_COUNT + 1):  # 5 недель: 4 прошлых + текущая
        monday = earliest_monday + timedelta(weeks=i)
        weeks.append(WeekSpan(monday=monday, sunday=monday + timedelta(days=6)))

    start_dt = datetime.combine(earliest_monday, datetime.min.time(), tzinfo=MSK)
    end_dt = datetime.combine(current_monday + timedelta(days=7),
                              datetime.min.time(), tzinfo=MSK)
    return start_dt, end_dt, weeks


# ---- project slug ----------------------------------------------------------

def project_from_workspace(workspace_dir: str | None) -> str | None:
    """Чистый slug проекта из workspaceDir, или None если это meta-workspace.

    Возвращает None (исключает проект из таблицы) для:
      - None / пустой строки
      - пути, у которого один из компонентов — `.mavis` или `.minimax`
        (служебные workspace'ы агента, не реальные проекты)

    Примеры:
      "C:/Projects/Python/0803_agent-tokens-dashboard" → "agent-tokens-dashboard"
      "C:/Projects/humans/0807_db-contingent"          → "db-contingent"
      "C:/Users/user/.mavis/agents/mavis/workspace"    → None  (meta)
      "C:/Users/user/.minimax/v2/..."                 → None  (meta)
      "C:/Projects/humans/foo"                         → "foo"
      ""                                               → None
      None                                             → None
    """
    if not workspace_dir:
        return None
    p = Path(workspace_dir)
    if any(part in _META_DIRS for part in p.parts):
        return None
    base = p.name
    if not base:
        return None
    return _DATE_PREFIX_RE.sub("", base, count=1)


# ---- aggregations ----------------------------------------------------------

def collect_projects(
    con: sqlite3.Connection, start_ts_ms: int, end_ts_ms: int
) -> list[ProjectRow]:
    """Собрать ProjectRow для окна [start_ts_ms, end_ts_ms).

    Источники:
      - local_runtime_message_rows — duration, requests, session_id список
      - local_runtime_token_usage — tokens (input+output)
      - local_runtime_sessions    — workspaceDir, status (active marker)

    Логика:
      1. Тянем message-агрегаты по session_id (min/max ts, user_msgs).
      2. Тянем tokens по session_id.
      3. Тянем session metadata (workspaceDir, status) для этих session_id.
      4. Группируем по project (slug из workspaceDir; meta → skip).
      5. Per project: max_ms=MAX, duration_ms=SUM, tokens=SUM,
         sessions=COUNT, is_active=ANY(status=='started').
      6. Default sort: max_ms DESC, tie-break tokens DESC. Финальный порядок
         строк юзер может поменять кликом по колонке (см. render_html).

    Edge cases:
      - Нет сообщений в окне → []
      - Все сессии с meta/None workspaceDir → []
      - Сессия с messages, но без token_usage → tokens += 0
      - Сессия без record_json (или битый JSON) → workspaceDir=None,
        status=None → проект skip, is_active=False.
    """
    # 1. Messages в окне.
    msg_sql = """
        SELECT session_id,
               MIN(created_at_ms) AS min_ms,
               MAX(created_at_ms) AS max_ms,
               SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS user_msgs
        FROM local_runtime_message_rows
        WHERE created_at_ms >= ? AND created_at_ms < ?
        GROUP BY session_id
    """
    msg_rows: dict[str, tuple[int, int, int]] = {}
    for sid, mn, mx, user in con.execute(msg_sql, (start_ts_ms, end_ts_ms)):
        msg_rows[str(sid)] = (int(mn), int(mx), int(user))

    if not msg_rows:
        return []

    # 2. Tokens в окне.
    tok_sql = """
        SELECT session_id,
               COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
        FROM local_runtime_token_usage
        WHERE ts >= ? AND ts < ?
        GROUP BY session_id
    """
    tok_rows: dict[str, int] = {}
    for sid, t in con.execute(tok_sql, (start_ts_ms, end_ts_ms)):
        tok_rows[str(sid)] = int(t)

    # 3. Sessions metadata.
    sids = list(msg_rows.keys())
    placeholders = ",".join("?" for _ in sids)
    sess_sql = f"""
        SELECT session_id, record_json
        FROM local_runtime_sessions
        WHERE session_id IN ({placeholders})
    """
    meta: dict[str, dict] = {}
    for sid, rec_json in con.execute(sess_sql, sids):
        try:
            rec = json.loads(rec_json)
        except (json.JSONDecodeError, TypeError):
            rec = {}
        meta[str(sid)] = rec

    # 4. Group by project.
    grouped: dict[str, dict] = {}
    for sid, (mn, mx, _user) in msg_rows.items():
        rec = meta.get(sid, {})
        workspace_dir = (
            rec.get("workspaceDir")
            if isinstance(rec.get("workspaceDir"), str)
            else None
        )
        status = rec.get("status")

        project = project_from_workspace(workspace_dir)
        if project is None:
            continue  # meta-workspace или пустой — пропускаем

        tokens = tok_rows.get(sid, 0)
        duration_ms = mx - mn  # > 0 гарантировано (есть MIN и MAX в одной группе)

        bucket = grouped.setdefault(project, {
            "max_ms": mx,
            "duration_ms": 0,
            "tokens": 0,
            "sessions": 0,
            "is_active": False,
        })
        if mx > bucket["max_ms"]:
            bucket["max_ms"] = mx
        bucket["duration_ms"] += duration_ms
        bucket["tokens"] += tokens
        bucket["sessions"] += 1
        if status == "started":
            bucket["is_active"] = True

    # 5. Build rows.
    rows: list[ProjectRow] = []
    for project, b in grouped.items():
        last_update_dt = datetime.fromtimestamp(b["max_ms"] / 1000, tz=MSK)
        rows.append(ProjectRow(
            project=project,
            last_update=last_update_dt.date(),
            max_ms=b["max_ms"],
            duration_ms=b["duration_ms"],
            tokens=b["tokens"],
            sessions=b["sessions"],
            is_active=b["is_active"],
        ))

    # 6. Sort: most recent first, tie-break tokens desc.
    rows.sort(key=lambda r: (r.max_ms, r.tokens), reverse=True)
    return rows


# ---- formatting ------------------------------------------------------------

def format_tokens(n: int) -> str:
    """Человеко-читаемое число токенов: 941.8K, 5.93M, 1.23B.

    Контракт precision (тот же, что в build_dashboard.py::fmt_tokens и
    build_session_dashboard.py::format_tokens):
      - K-шкала (1K..999K):  1 знак ("182.5K", "941.8K")
      - M-шкала (1M..999M):  2 знака ("1.23M", "5.93M")
      - B-шкала (1B+):       2 знака ("1.50B")
      - Под 1K: целое без суффикса.

    Trailing zero + точка стрипаются: 1.0K → 1K, 1.00M → 1M, 1.50M → 1.5M.
    Это согласовано с session-dashboard и нужно, чтобы "round" значения
    (1 миллион, 100 тысяч) не висели хвостом .00.
    """
    if n < 0:
        return "0"
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        num = f"{n / 1_000:.1f}"
        suffix = "K"
    elif n < 1_000_000_000:
        num = f"{n / 1_000_000:.2f}"
        suffix = "M"
    else:
        num = f"{n / 1_000_000_000:.2f}"
        suffix = "B"
    if "." in num:
        num = num.rstrip("0").rstrip(".")
    return num + suffix


def format_duration(ms: int) -> str:
    """Длительность в человеческом формате: 5m, 1h 4m, 2d 3h.

    Градация:
      - < 1 min   → "< 1m"
      - < 1 hour  → "Xm"   (round вниз)
      - < 1 day   → "Xh Ym" / "Xh" (если Ym=0)
      - >= 1 day  → "Xd Yh" / "Xd" (если Yh=0)

    На уровне проекта могут набегать дни (много сессий), поэтому ветка
    >= 1 day здесь задействована чаще, чем в session-dashboard.
    """
    if ms < 0:
        return "0m"
    total_min = ms // 60_000
    if total_min < 1:
        return "< 1m"
    if total_min < 60:
        return f"{total_min}m"
    total_h, rem_min = divmod(total_min, 60)
    if total_h < 24:
        if rem_min == 0:
            return f"{total_h}h"
        return f"{total_h}h {rem_min}m"
    days, rem_h = divmod(total_h, 24)
    if rem_h == 0:
        return f"{days}d"
    return f"{days}d {rem_h}h"


# Минимальный duration, на котором rate per hour имеет смысл. Ниже —
# экстраполяция с одного часа на минутный масштаб вводит в заблуждение
# ("30M/h" при 200 токенах за 30 секунд). Показываем "—".
RATE_MIN_DURATION_MS: int = 60_000


def format_rate(tokens: int, duration_ms: int) -> str:
    """Rate tokens/hour в человеческом формате: "1.23M/h", "335.7K/h", "400/h".

    Контракт:
      - duration_ms < RATE_MIN_DURATION_MS (== < 1 min) → "—". Rate per hour
        на минутном масштабе не определён; см. rate_sort_value для пары.
      - duration_ms >= RATE_MIN_DURATION_MS → "<format_tokens(rate)>/h",
        где rate = round(tokens * 3_600_000 / duration_ms). format_tokens
        даёт K/M/B precision (1dp/2dp/2dp) + trailing zero strip, тот же
        контракт, что в колонке TOKENS.
      - tokens=0 при duration > 0 → "0/h". Rate буквально нулевой, это
        не edge case.
      - Отрицательные токены (теоретически) → "0/h" через format_tokens.

    NB: rate считается от SUM активной duration по сессиям проекта (то же
    значение, что в колонке DURATION), а не от wall-clock между первой и
    последней сессией. Для проекта с пятью короткими сессиями суммарно по
    25 минут rate будет в 50+ раз выше, чем для проекта с одной сессией в
    1h с тем же количеством токенов. Это "интенсивность", не throughput.
    """
    if duration_ms < RATE_MIN_DURATION_MS:
        return "—"
    rate = int(round(tokens * 3_600_000 / duration_ms))
    return f"{format_tokens(rate)}/h"


def rate_sort_value(tokens: int, duration_ms: int) -> int:
    """Raw integer rate (tokens/hour) для client-side сортировки.

    Возвращает 0 при duration < RATE_MIN_DURATION_MS — "нет данных"
    сортируется в конец desc-таблицы и в начало asc-таблицы, что совпадает
    с визуальной позицией "—" в колонке.
    """
    if duration_ms < RATE_MIN_DURATION_MS:
        return 0
    return int(round(tokens * 3_600_000 / duration_ms))


# ---- render ----------------------------------------------------------------

def render_html(
    rows: list[ProjectRow], now_msk: datetime, weeks: list[WeekSpan]
) -> str:
    """Собрать self-contained HTML страницы с таблицей проектов.

    Стиль: тёмная тема, Inter, повторяет палитру --panel/--line/--accent из
    build_dashboard.py / build_session_dashboard.py. Карточка одна —
    project dashboard. Активные строки (is_active=True) помечены классом
    .active + бейджем "active" в первой колонке.
    """
    week_labels = ", ".join(w.label for w in weeks)
    projects_total = len(rows)
    active_total = sum(1 for r in rows if r.is_active)
    sessions_total = sum(r.sessions for r in rows)
    tokens_total = sum(r.tokens for r in rows)

    # Table rows.
    # `data-col` + `data-sort` на каждом <td> — контракт для client-side
    # сортировки (см. <script> в render_html). Raw-значения в data-sort,
    # formatted-версии остаются в тексте ячейки ("1h 39m" → "5940000",
    # "8.81M" → "8810000", "1.23M/h" → "1230000"). Это развязывает
    # форматирование и сортировку.
    body_rows: list[str] = []
    for r in rows:
        cls = ' class="active"' if r.is_active else ""
        project_esc = html.escape(r.project)
        date_esc = html.escape(r.last_update.isoformat())
        dur_esc = html.escape(format_duration(r.duration_ms))
        tok_esc = html.escape(format_tokens(r.tokens))
        rate_esc = html.escape(format_rate(r.tokens, r.duration_ms))
        rate_raw = rate_sort_value(r.tokens, r.duration_ms)
        sess_esc = str(int(r.sessions))
        badge = '<span class="badge">active</span>' if r.is_active else ""

        # data-sort: ISO date для last_update, raw int для остальных метрик,
        # raw slug для project (localeCompare в JS).
        date_sort = r.last_update.isoformat()  # "YYYY-MM-DD" — ISO-лексикографически = хронологически
        body_rows.append(
            f"      <tr{cls}>"
            f"<td class=\"project\" data-col=\"project\" data-sort=\"{project_esc}\">{project_esc}{badge}</td>"
            f"<td class=\"r\" data-col=\"last_update\" data-sort=\"{date_sort}\">{date_esc}</td>"
            f"<td class=\"r\" data-col=\"duration\" data-sort=\"{r.duration_ms}\">{dur_esc}</td>"
            f"<td class=\"r\" data-col=\"tokens\" data-sort=\"{r.tokens}\">{tok_esc}</td>"
            f"<td class=\"r\" data-col=\"rate\" data-sort=\"{rate_raw}\">{rate_esc}</td>"
            f"<td class=\"r\" data-col=\"sessions\" data-sort=\"{r.sessions}\">{sess_esc}</td>"
            f"</tr>"
        )
    body_html = "\n".join(body_rows) if body_rows else (
        '      <tr><td colspan="6" class="empty center">'
        "Нет проектов в окне</td></tr>"
    )

    meta_str = f"обновлено: {now_msk.strftime('%Y-%m-%d %H:%M')} MSK"
    footer_left = (
        f"{projects_total} projects · {sessions_total} sessions · "
        f"{len(weeks)} weeks ({week_labels})"
    )
    if active_total:
        footer_left += f" · {active_total} active"
    footer_right = f"{format_tokens(tokens_total)} tokens · MSK (UTC+3)"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="refresh" content="60" />
  <title>Project Dashboard — {now_msk.strftime('%Y-%m-%d %H:%M')} MSK</title>
  <style>
    :root {{
      --bg: #0f1115;
      --panel: #181b22;
      --panel-2: #1d2129;
      --ink: #f5f7fb;
      --muted: rgba(216, 223, 236, 0.62);
      --line: rgba(255, 255, 255, 0.05);
      --grid: rgba(148, 163, 184, 0.12);
      --accent: #8b5cf6;
      --accent-2: #10b981;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Inter", "Segoe UI Variable", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(139, 92, 246, 0.16), transparent 26%),
        linear-gradient(180deg, #0e1014 0%, #11141a 100%);
      min-height: 100vh;
      padding: 32px 28px 48px;
    }}
    .card {{
      max-width: 1280px;
      margin: 0 auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 22px 26px 18px;
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.02) inset;
    }}
    .head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      margin-bottom: 18px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--line);
    }}
    .title {{
      font-size: 12px;
      letter-spacing: 0.20em;
      text-transform: uppercase;
      color: var(--ink);
      font-weight: 600;
    }}
    .meta {{
      font-size: 11px;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-variant-numeric: tabular-nums;
    }}
    thead th {{
      text-align: left;
      font-weight: 500;
      font-size: 10px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
    }}
    thead th.r {{ text-align: right; }}
    thead th.sortable {{
      cursor: pointer;
      user-select: none;
      outline: none;
    }}
    thead th.sortable:hover {{ color: var(--ink); }}
    thead th.sortable:focus-visible {{
      box-shadow: inset 0 0 0 1px var(--accent);
    }}
    thead th.sortable.sorted {{ color: var(--ink); }}
    thead th .sort-ind {{
      display: inline-block;
      margin-left: 4px;
      width: 8px;
      color: var(--accent);
      font-size: 9px;
    }}
    tbody td {{
      padding: 10px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      color: var(--ink);
    }}
    tbody td.r {{ text-align: right; }}
    tbody td.project {{
      color: var(--ink);
      font-size: 13px;
      font-weight: 500;
    }}
    tbody tr:hover {{ background: rgba(255, 255, 255, 0.02); }}
    tbody tr.active td {{ background: rgba(139, 92, 246, 0.07); }}
    tbody tr.active td:first-child {{
      box-shadow: inset 2px 0 0 var(--accent);
    }}
    .badge {{
      display: inline-block;
      margin-left: 8px;
      padding: 1px 6px;
      border-radius: 4px;
      font-size: 9px;
      letter-spacing: 0.10em;
      text-transform: uppercase;
      background: rgba(139, 92, 246, 0.18);
      color: #b794f4;
      vertical-align: middle;
    }}
    .empty {{ color: var(--muted); }}
    .center {{ text-align: center; padding: 32px 10px; }}
    .footer {{
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      font-size: 10px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="head">
      <div class="title">project dashboard</div>
      <div class="meta">{meta_str}</div>
    </div>
    <table>
      <thead>
        <tr>
          <th class="sortable" data-col="project" tabindex="0" role="button" aria-sort="none">Project<span class="sort-ind"></span></th>
          <th class="sortable r" data-col="last_update" tabindex="0" role="button" aria-sort="none">Last Update<span class="sort-ind"></span></th>
          <th class="sortable r" data-col="duration" tabindex="0" role="button" aria-sort="none">Duration<span class="sort-ind"></span></th>
          <th class="sortable r" data-col="tokens" tabindex="0" role="button" aria-sort="none">Tokens<span class="sort-ind"></span></th>
          <th class="sortable r" data-col="rate" tabindex="0" role="button" aria-sort="none">Tok/Hour<span class="sort-ind"></span></th>
          <th class="sortable r" data-col="sessions" tabindex="0" role="button" aria-sort="none">Session<span class="sort-ind"></span></th>
        </tr>
      </thead>
      <tbody>
{body_html}
      </tbody>
    </table>
    <div class="footer">
      <span>{html.escape(footer_left)}</span>
      <span>{html.escape(footer_right)}</span>
    </div>
  </div>
  <script>
    // Client-side column sorting. Self-contained, no deps.
    //
    // Контракт:
    //   - <th data-col="..." class="sortable">  — кликабельный заголовок.
    //   - <td data-col="..." data-sort="<raw>"> — raw-значение для сортировки.
    //   - State в localStorage["agent-tokens-dashboard:sort"] как JSON
    //     {{"col": "<col>", "dir": "asc"|"desc"}}. Переживает 60s meta-refresh.
    //   - Default при пустом state: last_update desc (повторяет Python-дефолт).
    //   - Click по активной колонке → toggle dir. По другой → dir по типу
    //     колонки (project=asc, остальные=desc). Tie-breaker глобальный:
    //     last_update desc.
    (function () {{
      "use strict";
      var STORAGE_KEY = "agent-tokens-dashboard:sort";
      var DEFAULT_STATE = {{ col: "last_update", dir: "desc" }};
      var DEFAULT_DIR = {{
        project: "asc",
        last_update: "desc",
        duration: "desc",
        tokens: "desc",
        rate: "desc",
        sessions: "desc",
      }};
      var VALID_COLS = Object.keys(DEFAULT_DIR);

      function readState() {{
        try {{
          var raw = localStorage.getItem(STORAGE_KEY);
          if (!raw) return DEFAULT_STATE;
          var s = JSON.parse(raw);
          if (!s || VALID_COLS.indexOf(s.col) === -1) return DEFAULT_STATE;
          if (s.dir !== "asc" && s.dir !== "desc") return DEFAULT_STATE;
          return s;
        }} catch (e) {{
          return DEFAULT_STATE;
        }}
      }}

      function writeState(s) {{
        try {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); }}
        catch (e) {{ /* private mode / disabled storage — sort работает in-memory */ }}
      }}

      function sortValueFor(row, col) {{
        var cell = row.querySelector('td[data-col="' + col + '"]');
        return cell ? cell.getAttribute("data-sort") : "";
      }}

      function compareRows(a, b, col) {{
        var ax = sortValueFor(a, col);
        var bx = sortValueFor(b, col);
        var cmp;
        if (col === "project") {{
          cmp = ax.localeCompare(bx);
        }} else {{
          var an = Number(ax), bn = Number(bx);
          cmp = an < bn ? -1 : an > bn ? 1 : 0;
        }}
        // Tie-breaker: last_update desc (ISO date → численное сравнение).
        if (cmp === 0) {{
          var aL = Number(sortValueFor(a, "last_update"));
          var bL = Number(sortValueFor(b, "last_update"));
          cmp = aL < bL ? 1 : aL > bL ? -1 : 0;
        }}
        return cmp;
      }}

      function sortBy(col, dir) {{
        var tbody = document.querySelector("table tbody");
        if (!tbody) return;
        var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
        var mul = dir === "desc" ? -1 : 1;
        rows.sort(function (a, b) {{ return compareRows(a, b, col) * mul; }});
        // appendChild перемещает существующий узел, не клонирует — порядок
        // в DOM меняется, ссылки на <tr> остаются валидными.
        for (var i = 0; i < rows.length; i++) {{
          tbody.appendChild(rows[i]);
        }}
      }}

      function updateIndicators(col, dir) {{
        var ths = document.querySelectorAll("thead th.sortable");
        for (var i = 0; i < ths.length; i++) {{
          var th = ths[i];
          var ind = th.querySelector(".sort-ind");
          if (th.getAttribute("data-col") === col) {{
            if (ind) ind.textContent = dir === "asc" ? "\\u25B2" : "\\u25BC";
            th.classList.add("sorted");
            th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
          }} else {{
            if (ind) ind.textContent = "";
            th.classList.remove("sorted");
            th.setAttribute("aria-sort", "none");
          }}
        }}
      }}

      function onHeaderClick(ev) {{
        var th = ev.currentTarget;
        var col = th.getAttribute("data-col");
        if (VALID_COLS.indexOf(col) === -1) return;
        var current = readState();
        var dir;
        if (current.col === col) {{
          dir = current.dir === "asc" ? "desc" : "asc";
        }} else {{
          dir = DEFAULT_DIR[col] || "desc";
        }}
        var next = {{ col: col, dir: dir }};
        writeState(next);
        sortBy(col, dir);
        updateIndicators(col, dir);
      }}

      function onHeaderKey(ev) {{
        // Enter / Space — то же, что click. Без preventDefault для Enter
        // (форма не submit'ится, всё ОК).
        if (ev.key === "Enter" || ev.key === " ") {{
          ev.preventDefault();
          onHeaderClick(ev);
        }}
      }}

      function init() {{
        var ths = document.querySelectorAll("thead th.sortable");
        for (var i = 0; i < ths.length; i++) {{
          ths[i].addEventListener("click", onHeaderClick);
          ths[i].addEventListener("keydown", onHeaderKey);
        }}
        var s = readState();
        sortBy(s.col, s.dir);
        updateIndicators(s.col, s.dir);
      }}

      if (document.readyState === "loading") {{
        document.addEventListener("DOMContentLoaded", init);
      }} else {{
        init();
      }}
    }})();
  </script>
</body>
</html>
"""


# ---- main ------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    db_path: Path = args.db
    out_path: Path = args.out
    quiet: bool = args.quiet

    now_msk = datetime.now(MSK)
    today = now_msk.date()

    _, end_dt, weeks = compute_window(today)
    start_dt = datetime.combine(weeks[0].monday, datetime.min.time(), tzinfo=MSK)
    start_ts_ms = int(start_dt.timestamp() * 1000)
    # Реальный SQL-фильтр режется по now_msk, не по end_dt (конец текущей
    # недели). Это нужно, чтобы future-dated строки не попали в таблицу, и
    # чтобы "позже вечером" та же сборка дала больше сессий за текущий день
    # без правок кода.
    end_ts_ms = int(now_msk.timestamp() * 1000)

    con = open_db(db_path)
    try:
        rows = collect_projects(con, start_ts_ms, end_ts_ms)
    finally:
        con.close()

    html_doc = render_html(rows, now_msk, weeks)

    if args.no_write:
        sys.stdout.write(html_doc)
        return 0

    out_path.write_text(html_doc, encoding="utf-8")

    if not quiet:
        active_n = sum(1 for r in rows if r.is_active)
        sessions_n = sum(r.sessions for r in rows)
        print(
            f"[project-dashboard] window=[{start_dt.date()}..{end_dt.date()}) "
            f"weeks=[{','.join(w.label for w in weeks)}] "
            f"projects={len(rows)} sessions={sessions_n} active={active_n} "
            f"→ {out_path}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
