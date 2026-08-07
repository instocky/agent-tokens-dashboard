"""build_session_dashboard.py — static per-session dashboard for the last 4 ISO weeks.

Читает `local_runtime_token_usage`, `local_runtime_message_rows` и
`local_runtime_sessions` из runtime-state.sqlite и собирает self-contained
`session-dashboard.html` (без backend, без внешнего JSON).

Окно: 4 последние завершённых ISO-недели (Пн–Вс) + текущая (5 недель всего).
  Например, для today=2026-08-07 (W-32) концептуальное окно = [2026-07-06,
  2026-08-10) MSK, т.е. W-28..W-32. Реальный SQL-фильтр обрезается по now_msk
  в main() — будущих дней в БД нет, так что это эквивалентно "включена вся
  текущая неделя до текущего момента".

Сортировка: сверху самая свежая (по MAX created_at_ms в окне).

Активные сессии: status='started' в record_json помечаются бейджем "active" и
лёгкой акцентной строкой. Включаются только если у сессии есть activity
(created_at_ms в message_rows) в окне — иначе сессия не попадает в таблицу
по тому же фильтру, что и finished.

Запуск:  python build_session_dashboard.py
Опции:   --db <path>     путь к sqlite (по умолчанию DB_PATH ниже)
         --out <path>    путь к выходному HTML (по умолчанию OUTPUT_PATH ниже)
         --no-write      не записывать файл (dry-run, печатает в stdout)
         --quiet         не печатать лог

Расписание: Windows Task Scheduler, раз в 5 минут. Отдельная задача от
build_dashboard.py — этот скрипт собирает другой артефакт (session-dashboard.html)
с другим временем жизни. Можно повесить на ту же 5-минутку или на свой крон.
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
OUTPUT_PATH: Path = Path(__file__).resolve().parent / "session-dashboard.html"

# Europe/Moscow = UTC+3 круглый год (с 2014 без перехода на зимнее время).
# Хардкод константой, как и просили — без zoneinfo-зависимостей.
MSK = timezone(timedelta(hours=3))

# Размер окна — 4 завершённых ISO-недели + текущая (5 недель всего).
# Текущая неделя включается частично: фильтр обрезается по `now_msk` в main(),
# так что "будущие" дни текущей недели автоматически отсутствуют (в БД их
# просто нет). См. compute_window() и main().
WEEK_COUNT: int = 4

# Префикс даты в имени директории (формат YYYY_), который скрываем в колонке
# "Project", чтобы вытащить чистый slug. Пример: "0807_db-contingent" →
# "db-contingent". Если префикса нет — basename возвращается как есть.
# Захватываем только ведущие 4 цифры + "_" — никаких "23_" или "2024_",
# только фиксированный 4-значный год + подчёркивание.
_DATE_PREFIX_RE = re.compile(r"^\d{4}_")


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
class SessionRow:
    """Одна строка таблицы session dashboard.

    Все даты — MSK. `max_ms` нужен для сортировки "свежие сверху"; это
    MAX(created_at_ms) в окне, а не end_msk (end_msk — это date, без
    внутридневной точности, при равных date сломает порядок).
    """
    session_id: str
    title: str | None
    project: str | None
    workspace_dir: str | None
    start_msk: date
    end_msk: date
    max_ms: int
    duration_ms: int
    tokens: int
    requests: int
    is_active: bool


# ---- CLI -------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Сгенерировать self-contained session-dashboard.html из runtime-state.sqlite.",
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
    """4 завершованных ISO-недели (Пн–Вс) + текущая = 5 недель всего.

    Возвращает (start_dt, end_dt, weeks), где:
      - start_dt — MSK midnight понедельника самой ранней недели окна
      - end_dt   — MSK midnight понедельника следующей недели (exclusive, т.е.
                   конец воскресенья текущей недели включительно)
      - weeks    — список WeekSpan, oldest-first (W-28, W-29, W-30, W-31, W-32)

    Контракт "4 завершённых + текущая":
      current_monday  = today − (isoweekday − 1)
      earliest_monday = current_monday − 4 weeks
      weeks = [earliest_monday, ..., current_monday]  (5 штук)
      Концептуальное окно: [earliest_monday 00:00, current_monday + 7d 00:00).

    Реальный SQL-фильтр в main() обрезается по now_msk, не по end_dt —
    это нужно, чтобы "будущие" дни текущей недели автоматически не попали
    (в БД их нет, но явный clamp защищает от дрифта часов / future-dated
    записей при импорте данных).
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
    """Чистый slug проекта из workspaceDir.

    Примеры:
      "C:/Projects/kamkb/0807_db-contingent"          → "db-contingent"
      "C:\\\\Projects\\\\Python\\\\0803_agent-tokens-dashboard" → "agent-tokens-dashboard"
      "C:/Projects/humans/foo"                         → "foo"  (без префикса — as-is)
      "C:/Users/user/Documents"                        → "Documents"
      ""                                               → None
      None                                             → None

    Логика: Path(...).name даёт basename (работает и для '/', и для '\\' на
    Windows). Затем стрипаем ведущие 4 цифры + '_' (ровно одну итерацию —
    replace_all=False через count=1 в re.sub, чтобы случайный "_" в середине
    имени не тронуть).
    """
    if not workspace_dir:
        return None
    base = Path(workspace_dir).name
    if not base:
        return None
    return _DATE_PREFIX_RE.sub("", base, count=1)


# ---- aggregations ----------------------------------------------------------

def collect_sessions(
    con: sqlite3.Connection, start_ts_ms: int, end_ts_ms: int
) -> list[SessionRow]:
    """Собрать SessionRow для окна [start_ts_ms, end_ts_ms).

    Источники:
      - local_runtime_message_rows — duration, requests (role='user'), даты
      - local_runtime_token_usage — tokens (input+output)
      - local_runtime_sessions    — title, workspaceDir, status (active marker)

    Сессия попадает в таблицу, если у неё есть ХОТЯ БЫ ОДНО сообщение
    (любой роли) в окне. Это сознательно: "session that happened in the
    4-week window". Сессия, у которой есть только token_usage без messages,
    сюда не попадёт (таких не бывает — turn_id в token_usage всегда
    соответствует user-turn'у, у которого есть message_row).

    Edge cases:
      - Нет сообщений в окне → []
      - Сессия с messages, но без token_usage → tokens=0
      - Сессия с messages, но без user-сообщений → requests=0
      - Сессия без record_json (или битый JSON) → title=None, project=None,
        is_active=False. На рендере уйдёт в "—" / "active" не покажется.
    """
    # 1. Messages в окне — даёт duration, requests, и сразу session_id список.
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

    # 2. Tokens в окне — отдельный GROUP BY (быстрее, чем JOIN с messages).
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

    # 3. Sessions metadata — title, workspaceDir, status. IN(...) с плейсхолдерами.
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

    # 4. Assemble.
    rows: list[SessionRow] = []
    for sid, (mn, mx, user) in msg_rows.items():
        rec = meta.get(sid, {})
        title = rec.get("title") if isinstance(rec.get("title"), str) else None
        workspace_dir = rec.get("workspaceDir") if isinstance(rec.get("workspaceDir"), str) else None
        status = rec.get("status")

        start_dt = datetime.fromtimestamp(mn / 1000, tz=MSK)
        end_dt = datetime.fromtimestamp(mx / 1000, tz=MSK)
        duration_ms = mx - mn  # > 0 гарантировано (есть MIN и MAX в одной группе)

        rows.append(SessionRow(
            session_id=sid,
            title=title,
            project=project_from_workspace(workspace_dir),
            workspace_dir=workspace_dir,
            start_msk=start_dt.date(),
            end_msk=end_dt.date(),
            max_ms=mx,
            duration_ms=duration_ms,
            tokens=tok_rows.get(sid, 0),
            requests=user,
            is_active=(status == "started"),
        ))

    # 5. Sort: most recent first (по MAX created_at_ms desc).
    rows.sort(key=lambda r: r.max_ms, reverse=True)
    return rows


# ---- formatting ------------------------------------------------------------

def format_tokens(n: int) -> str:
    """Человеко-читаемое число токенов: 941.8K, 5.93M, 1.23B.

    Тот же контракт precision, что и build_dashboard.py::fmt_tokens:
    - K-шкала (1K..999K): 1 знак после запятой ("182.5K", "941.8K")
    - M-шкала (1M..999M): 2 знака ("1.23M", "5.93M")
    - B-шкала (1B+):       2 знака ("1.50B")
    - Под 1K: целое без суффикса.

    Trailing zero + точка стрипаются: 1.0K → 1K, 1.00M → 1M, 1.50M → 1.5M.
    Это согласовано с fmt_log_tick в build_dashboard.py и нужно чтобы
    "round" значения (1 миллион, 100 тысяч) не висели хвостом .00.

    Реализация: снимаем суффикс (K/M/B), стрипаем zero+dot на числовой
    части, склеиваем обратно. s.rstrip("0") напрямую не сработает — он
    упрётся в суффикс и ничего не отрежет.
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
      - < 1 hour  → "Xm"   (round вниз, минута — наименьшая единица)
      - < 1 day   → "Xh Ym" / "Xh" (если Ym=0)
      - >= 1 day  → "Xd Yh" / "Xd" (если Yh=0)

    Округление: целочисленные минуты/часы (округление вниз). Сессия
    длиной 1h 59m 59s показывается как "1h 59m" — не "2h". Это сознательно:
    короткосессионный display, не точный хронометр. Если нужна секундная
    точность — открывай JSON, а не таблицу.
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


def format_date_cell(start: date, end: date) -> str:
    """Дата (или диапазон) для колонки Date.

    - same day             → "2026-08-07"
    - разные дни (≤ 6 days) → "2026-08-05 → 2026-08-07"
    - >= 7 days             → "2026-08-05 → 2026-08-12 (8d)"

    Многодневные сессии редки, но бывают (ночная сессия, переход через
    полночь, или branch-session с длинной паузой). Показываем диапазон
    явно, без сокращений — лучше перестараться, чем потерять контекст.
    """
    if start == end:
        return start.isoformat()
    span_days = (end - start).days + 1
    if span_days < 7:
        return f"{start.isoformat()} → {end.isoformat()}"
    return f"{start.isoformat()} → {end.isoformat()} ({span_days}d)"


def title_display(row: SessionRow) -> str:
    """Title для рендера. Fallback на хвост session_id, если title пустой."""
    if row.title:
        return row.title
    # mvs_fa976650d8ab451a915fd47e80e2b14f → "f...80e2b14f" (8 chars tail).
    # Скругляем до 8, чтобы ширина колонки не прыгала.
    return "…" + row.session_id[-8:]


# ---- render ----------------------------------------------------------------

def render_html(rows: list[SessionRow], now_msk: datetime, weeks: list[WeekSpan]) -> str:
    """Собрать self-contained HTML страницы с таблицей сессий.

    Стиль: тёмная тема, Inter, повторяет палитру --panel/--line/--accent из
    build_dashboard.py. Карточка одна — session dashboard. Активные строки
    помечены классом .active + бейджем "active" в первой колонке.
    """
    week_labels = ", ".join(w.label for w in weeks)
    sessions_total = len(rows)
    active_total = sum(1 for r in rows if r.is_active)

    # Table rows.
    body_rows: list[str] = []
    for r in rows:
        cls = ' class="active"' if r.is_active else ""
        title_esc = html.escape(title_display(r))
        project_esc = html.escape(r.project) if r.project else '<span class="empty">—</span>'
        date_esc = html.escape(format_date_cell(r.start_msk, r.end_msk))
        dur_esc = html.escape(format_duration(r.duration_ms))
        tok_esc = html.escape(format_tokens(r.tokens))
        req_esc = str(int(r.requests))
        badge = '<span class="badge">active</span>' if r.is_active else ""

        body_rows.append(
            f"      <tr{cls}>"
            f"<td class=\"title\">{title_esc}{badge}</td>"
            f"<td class=\"project\">{project_esc}</td>"
            f"<td class=\"r\">{date_esc}</td>"
            f"<td class=\"r\">{dur_esc}</td>"
            f"<td class=\"r\">{tok_esc}</td>"
            f"<td class=\"r\">{req_esc}</td>"
            f"</tr>"
        )
    body_html = "\n".join(body_rows) if body_rows else (
        '      <tr><td colspan="6" class="empty center">'
        "Нет сессий в окне</td></tr>"
    )

    meta_str = f"обновлено: {now_msk.strftime('%Y-%m-%d %H:%M')} MSK"
    footer_left = f"{sessions_total} sessions · {len(weeks)} weeks ({week_labels})"
    if active_total:
        footer_left += f" · {active_total} active"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="refresh" content="60" />
  <title>Session Dashboard — {now_msk.strftime('%Y-%m-%d %H:%M')} MSK</title>
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
    tbody td {{
      padding: 10px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      color: var(--ink);
    }}
    tbody td.r {{ text-align: right; }}
    tbody td.title {{
      max-width: 360px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    tbody td.project {{
      color: var(--muted);
      font-size: 12px;
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
      <div class="title">session dashboard</div>
      <div class="meta">{meta_str}</div>
    </div>
    <table>
      <thead>
        <tr>
          <th>Title</th>
          <th>Project</th>
          <th class="r">Date</th>
          <th class="r">Duration</th>
          <th class="r">Tokens</th>
          <th class="r">Requests</th>
        </tr>
      </thead>
      <tbody>
{body_html}
      </tbody>
    </table>
    <div class="footer">
      <span>{html.escape(footer_left)}</span>
      <span>MSK (UTC+3)</span>
    </div>
  </div>
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
    # недели). Это нужно, чтобы future-dated строки (если такие когда-то
    # появятся в БД) не попали в таблицу, и чтобы "позже вечером" та же
    # сборка дала больше сессий за текущий день без правок кода.
    end_ts_ms = int(now_msk.timestamp() * 1000)

    con = open_db(db_path)
    try:
        rows = collect_sessions(con, start_ts_ms, end_ts_ms)
    finally:
        con.close()

    html_doc = render_html(rows, now_msk, weeks)

    if args.no_write:
        sys.stdout.write(html_doc)
        return 0

    out_path.write_text(html_doc, encoding="utf-8")

    if not quiet:
        active_n = sum(1 for r in rows if r.is_active)
        print(
            f"[session-dashboard] window=[{start_dt.date()}..{end_dt.date()}) "
            f"weeks=[{','.join(w.label for w in weeks)}] "
            f"sessions={len(rows)} active={active_n} "
            f"→ {out_path}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
