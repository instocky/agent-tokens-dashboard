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
    MAX(created_at_ms) среди сессий проекта в окне. `time_series` — None
    для проектов без token_usage в окне (такие строки без шеврона).
    """
    project: str
    last_update: date
    max_ms: int
    duration_ms: int
    tokens: int
    sessions: int
    is_active: bool
    time_series: "ProjectTimeSeries | None" = None


@dataclass(frozen=True)
class ProjectTimeSeries:
    """Per-project time series для inline-зоны под строкой таблицы.

    `days`  — {msk_date: tokens} для всех дней в окне (включая пустые —
              рендер показывает их как empty bar). Гарантированно покрывает
              непрерывный диапазон [first_day .. last_day] без пропусков,
              т.е. пустые дни между активностями тоже присутствуют с value=0.
    `hours` — {(msk_date, msk_hour): tokens} для 24h stream выбранного дня.
    `first_day` / `last_day` — MSK dates, ограничивают дневной ряд. None,
              если в окне вообще нет token_usage (тогда dataclass не
              создаётся, ProjectRow.time_series = None).
    """
    days: dict[date, int]
    hours: dict[tuple[date, int], int]
    first_day: date
    last_day: date


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
) -> tuple[list[ProjectRow], dict[str, str]]:
    """Собрать ProjectRow для окна [start_ts_ms, end_ts_ms).

    Возвращает (rows, sid_to_project), где sid_to_project — маппинг
    session_id → project slug для всех сессий, прошедших meta-workspace
    filter. Используется collect_time_series для join'а token_usage по
    project без повторного SQL на sessions.

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
      - Нет сообщений в окне → ([], {})
      - Все сессии с meta/None workspaceDir → ([], {})
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
        return [], {}

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
    sid_to_project: dict[str, str] = {}
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

        # Параллельно строим sid→project для collect_time_series.
        sid_to_project[sid] = project

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
    return rows, sid_to_project


def collect_time_series(
    con: sqlite3.Connection,
    start_ts_ms: int,
    end_ts_ms: int,
    sid_to_project: dict[str, str],
) -> dict[str, ProjectTimeSeries]:
    """Per-project per-day и per-(day,hour) агрегаты токенов в окне.

    Возвращает {project: ProjectTimeSeries} только для проектов с ≥ 1 turn'ом
    token_usage в окне. Проекты без token_usage в окне в словаре отсутствуют —
    в ProjectRow.time_series для них остаётся None (строка без шеврона).

    `sid_to_project` — маппинг session_id → project slug, построенный тем же
    join'ом что и collect_projects (workspaceDir → slug). Передаётся извне,
    чтобы не дублировать SQL на sessions.

    Дневной ряд делается непрерывным [first_day..last_day] — пустые дни между
    активностями тоже входят с value=0, чтобы дневной чарт не «прыгал»
    по шкале и не терял единый визуальный ритм.

    Edge cases:
      - Пустой sid_to_project (нет сессий в окне) → {}.
      - Turn'ы от session_id, которых нет в sid_to_project (битый join) →
        skip. Не должно случаться при корректном вызове из main().
      - Turn'ы с ts вне окна (будущее или до start_ts_ms) → 0, потому что
        SQL уже отрезал по WHERE ts >= ? AND ts < ?.
    """
    if not sid_to_project:
        return {}

    # Один SQL: GROUP BY session_id, msk_date, msk_hour. Локальное
    # преобразование ms→MSK (+3 hours) делаем в SQLite — детерминированно,
    # не зависит от локали машины. Шаблон идентичен build_dashboard.py::
    # aggregate_by_hour.
    sql = """
        SELECT
            session_id,
            date(ts / 1000, 'unixepoch', '+3 hours')                          AS msk_date,
            CAST(strftime('%H', ts / 1000, 'unixepoch', '+3 hours') AS INT)  AS msk_hour,
            COALESCE(SUM(input_tokens + output_tokens), 0)                    AS tokens
        FROM local_runtime_token_usage
        WHERE ts >= ? AND ts < ?
        GROUP BY session_id, msk_date, msk_hour
    """

    # Промежуточные буферы: per-project словари day→sum и (day,hour)→sum.
    days_buf: dict[str, dict[date, int]] = {}
    hours_buf: dict[str, dict[tuple[date, int], int]] = {}
    first_day: dict[str, date] = {}
    last_day: dict[str, date] = {}

    for sid, d_str, h, tokens in con.execute(sql, (start_ts_ms, end_ts_ms)):
        sid_s = str(sid)
        project = sid_to_project.get(sid_s)
        if project is None:
            continue
        d = date.fromisoformat(str(d_str))
        hour = int(h)
        t = int(tokens)
        if t <= 0:
            continue

        d_b = days_buf.setdefault(project, {})
        d_b[d] = d_b.get(d, 0) + t

        h_b = hours_buf.setdefault(project, {})
        h_b[(d, hour)] = h_b.get((d, hour), 0) + t

        if project not in first_day or d < first_day[project]:
            first_day[project] = d
        if project not in last_day or d > last_day[project]:
            last_day[project] = d

    # Дополняем дневной ряд пустыми днями в [first_day..last_day]. Это
    # гарантирует непрерывную X-шкалу для чарта.
    result: dict[str, ProjectTimeSeries] = {}
    for project, d_b in days_buf.items():
        fd = first_day[project]
        ld = last_day[project]
        # Сборка полного days dict
        full_days: dict[date, int] = {}
        d = fd
        while d <= ld:
            full_days[d] = d_b.get(d, 0)
            d += timedelta(days=1)
        result[project] = ProjectTimeSeries(
            days=full_days,
            hours=hours_buf.get(project, {}),
            first_day=fd,
            last_day=ld,
        )
    return result


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

# Короткая подпись дня в формате "13 авг" (DD + сокращённый русский месяц).
# Используется в дневном ряду inline-зоны и в заголовке выбранного дня.
_MONTHS_RU_SHORT: tuple[str, ...] = (
    "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)


def format_day_short(d: date) -> str:
    """DD MMM (рус.), напр. '13 авг', '1 сен'. Без leading zero на дне."""
    return f"{d.day} {_MONTHS_RU_SHORT[d.month - 1]}"


def format_day_iso(d: date) -> str:
    """ISO 'YYYY-MM-DD' для data-атрибутов. Дефолт Python isoformat()."""
    return d.isoformat()


def format_pct(value: int, total: int) -> str:
    """Доля в процентах, целое число без '%'. total=0 → '0'."""
    if total <= 0:
        return "0"
    pct = int(round(value * 100 / total))
    if pct < 0:
        return "0"
    if pct > 100:
        return "100"
    return str(pct)


# Лог- или linear шкала для дневного ряда. На данных проекта 0807_db-contingent
# (4 дня, max=122M, min=12M, ratio 10x) линейная шкала сплющивает малые дни
# до ~10% от высоты — плохо читается. На 0717_docstudio (4 дня, ratio 1.9x)
# разница между шкалами минимальна. Решаем per-project: ratio > 5x → log.
LOG_SCALE_RATIO_THRESHOLD: float = 5.0


def bar_height_pct(value: int, max_value: int, use_log: bool) -> float:
    """Высота бара в процентах (0..100) для per-project normalized Y.

    Linear:  value / max * 100, floor 2% (как в build_dashboard.py::_pct).
    Log:     log(1+v) / log(1+max) * 100, floor 2%. Используется при
             max_value / min_non_zero > LOG_SCALE_RATIO_THRESHOLD.
    Empty:   max=0 → 0% (вся строка пустая, не бывает при наличии time_series).
    """
    if value <= 0 or max_value <= 0:
        return 0.0
    if use_log:
        import math
        return max(2.0, min(100.0, (math.log1p(value) / math.log1p(max_value)) * 100))
    return max(2.0, min(100.0, (value / max_value) * 100))


def render_project_detail(
    project_slug: str,
    ts: ProjectTimeSeries,
    total_tokens: int,
    now_msk: datetime,
) -> str:
    """HTML inline-зоны под строкой проекта: дневной ряд + 24h для выбранного дня.

    Структура (всё внутри одной <td colspan="6">):
      .detail-inner
        .detail-header         "N дней · всего X · пик DD MMM (Y)"
        .day-chart             грид-баров (1..N дней)
          .day-bars            row of N .day-bar элементов
          .day-labels          row of N .day-label элементов (DD)
        .day-separator         "DD MMM · total · X.XM" (про выбранный день)
        .hour-chart            сюда JS подменяет innerHTML при клике
          .chart-shell.chart-shell--24h
            .hours-24h         24 .hour-cell для initial выбранного дня
        <script type="application/json" class="day-hour-map">
          {"YYYY-MM-DD": {"0": N, ..., "23": N}, ...}
        </script>
        <script type="application/json" class="day-meta">
          {"max_value": N, "use_log": bool, "selected_day": "YYYY-MM-DD"}
        </script>

    Клик по .day-bar: JS читает .day-hour-map, обновляет .hour-chart и
    .day-separator, переключает класс .selected на барах.

    Per-project normalized Y (100% = max за этот проект). Лог-шкала включается
    автоматически если max/min (по ненулевым дням) > LOG_SCALE_RATIO_THRESHOLD.
    1-day проект: один full-width бар с подписью «единственный день активности»,
    24h рендерится сразу.
    """
    days_sorted: list[date] = sorted(ts.days.keys())
    if not days_sorted:
        return ""

    # max value по ненулевым дням для per-project Y.
    nonzero = [v for v in ts.days.values() if v > 0]
    if not nonzero:
        max_value = 0
        use_log = False
    else:
        max_value = max(nonzero)
        min_nonzero = min(nonzero)
        # ratio max/min: 0 если все дни равны (тогда log == linear)
        ratio = max_value / min_nonzero if min_nonzero > 0 else 1.0
        use_log = ratio > LOG_SCALE_RATIO_THRESHOLD

    # Peak day = день с максимальным value (для заголовка).
    peak_day = max(ts.days.items(), key=lambda kv: kv[1])[0] if max_value > 0 else days_sorted[0]
    peak_value = ts.days.get(peak_day, 0)

    # Initial выбранный день = самый свежий день с данными. Для 1-day проекта
    # это и есть единственный день.
    days_with_data = [d for d in days_sorted if ts.days.get(d, 0) > 0]
    selected_day = days_with_data[-1] if days_with_data else days_sorted[-1]
    is_one_day = len(days_sorted) == 1

    # Заголовок
    n_days_label = "1 день" if is_one_day else f"{len(days_sorted)} дней"
    peak_label = f"{format_day_short(peak_day)} ({format_tokens(peak_value)})"
    header = (
        f'<div class="detail-header">'
        f'{n_days_label} · всего {html.escape(format_tokens(total_tokens))}'
        f' · пик {html.escape(peak_label)}'
        f'</div>'
    )

    # Дневной ряд: один бар на день. height_pct считаем здесь, в Python —
    # max известен на момент сборки.
    day_bars: list[str] = []
    day_labels: list[str] = []
    for d in days_sorted:
        v = ts.days.get(d, 0)
        h_pct = bar_height_pct(v, max_value, use_log)
        is_peak = (v > 0 and v == max_value)
        is_selected = (d == selected_day)
        bar_cls = "day-bar"
        if v <= 0:
            bar_cls += " day-bar--empty"
        if is_peak:
            bar_cls += " day-bar--peak"
        if is_selected:
            bar_cls += " day-bar--selected"
        # Tooltip: "DD MMM · X.XM · NN%". Используем стандартный title=
        # чтобы не плодить кастомный tooltip-компонент.
        tip = (
            f"{format_day_short(d)} · {format_tokens(v)} · "
            f"{format_pct(v, total_tokens)}%"
        )
        day_bars.append(
            f'<div class="{bar_cls}" '
            f'data-day="{format_day_iso(d)}" '
            f'data-tokens="{v}" '
            f'style="height: {h_pct:.1f}%" '
            f'title="{html.escape(tip)}" '
            f'role="button" tabindex="0" aria-label="{html.escape(tip)}">'
            f'</div>'
        )
        # Лейбл: только день (без месяца) — месяц один на всю короткую серию.
        day_labels.append(f'<span class="day-label">{d.day:02d}</span>')

    one_day_caption = ""
    if is_one_day:
        one_day_caption = (
            f'<div class="day-caption">'
            f'единственный день активности · {html.escape(format_tokens(ts.days[days_sorted[0]]))}'
            f'</div>'
        )

    day_chart = (
        f'<div class="day-chart">'
        f'<div class="day-bars">{"".join(day_bars)}</div>'
        f'<div class="day-labels">{"".join(day_labels)}</div>'
        f'{one_day_caption}'
        f'</div>'
    )

    # Separator + initial 24h для выбранного дня
    sep_text = (
        f'{format_day_short(selected_day)} · '
        f'{html.escape(format_tokens(ts.days.get(selected_day, 0)))}'
    )
    day_separator = (
        f'<div class="day-separator" data-selected-day="{format_day_iso(selected_day)}">'
        f'{html.escape(sep_text)}'
        f'</div>'
    )
    hour_chart_initial = render_24h_for_day(ts, selected_day, now_msk)
    hour_chart = (
        f'<div class="hour-chart" data-project="{html.escape(project_slug)}">'
        f'{hour_chart_initial}'
        f'</div>'
    )

    # day-hour-map: {day_iso: {hour: tokens, ...}} для всех дней проекта.
    # Используется JS'ом при клике на day-bar — пересобирает .hour-chart.
    day_hour_map: dict[str, dict[str, int]] = {}
    for d in days_sorted:
        per_hour: dict[str, int] = {}
        for h in range(24):
            per_hour[str(h)] = int(ts.hours.get((d, h), 0))
        day_hour_map[format_day_iso(d)] = per_hour

    day_meta = {
        "max_value": int(max_value),
        "use_log": bool(use_log),
        "selected_day": format_day_iso(selected_day),
        "total_tokens": int(total_tokens),
    }

    detail_inner = (
        f'<div class="detail-inner">'
        f'{header}'
        f'{day_chart}'
        f'{day_separator}'
        f'{hour_chart}'
        f'<script type="application/json" class="day-hour-map">'
        f'{html.escape(json.dumps(day_hour_map, ensure_ascii=False))}'
        f'</script>'
        f'<script type="application/json" class="day-meta">'
        f'{html.escape(json.dumps(day_meta, ensure_ascii=False))}'
        f'</script>'
        f'</div>'
    )
    return detail_inner


def render_24h_for_day(
    ts: ProjectTimeSeries,
    target_day: date,
    now_msk: datetime,
) -> str:
    """24h stream для конкретного дня проекта (тот же визуал, что в
    build_dashboard.py::_render_24h_stream, но inline в project-dashboard).

    Семантика states для target_day:
      - target_day < today  → все 24 часа либо active (data>0), peak (top-1),
                              либо empty (data=0). Никаких future/current.
      - target_day == today → стандартные active/peak/current/future/empty
                              относительно now_msk.hour.
      - target_day > today  → не должно случаться (окно обрезано по now_msk).
    """
    target_iso = format_day_iso(target_day)
    today_msk_date = now_msk.date()
    is_today = (target_day == today_msk_date)

    # Достаём 24 значений
    values: list[int] = []
    for h in range(24):
        values.append(int(ts.hours.get((target_day, h), 0)))

    # peak: top-1 value > 0
    peak_value = max(values) if values else 0
    peak_hours: set[int] = set()
    if peak_value > 0:
        for h, v in enumerate(values):
            if v == peak_value:
                peak_hours.add(h)
                break  # берём первый (наименьший hour) — детерминированно

    # scale_max для active/current/peak. Если всё пусто — 1 (защита от /0).
    if is_today:
        past_values = [
            values[h] for h in range(now_msk.hour + 1)
        ]
    else:
        past_values = list(values)  # все 24 часа — "прошлые" для не-сегодня
    scale_max = max(past_values) if past_values else 1
    if scale_max <= 0:
        scale_max = 1

    def pct(v: int) -> float:
        if v <= 0 or scale_max <= 0:
            return 0.0
        return max(2.0, min(100.0, (v / scale_max) * 100))

    cells: list[str] = []
    for h in range(24):
        v = values[h]
        # state
        if is_today:
            if h < now_msk.hour:
                state = "active"
            elif h == now_msk.hour:
                state = "current"
            else:
                state = "future"
        else:
            # Past day
            state = "active"

        if h in peak_hours:
            state = "peak"  # override
        if state in ("active", "current") and v <= 0:
            state = "empty"

        cls = f"bar-24h {state}"
        h_pct = pct(v)

        title = f"{h:02d}:00–{h:02d}:59: {format_tokens(v) if v else 'нет данных'}"
        label_cls = "hour-label"
        if state == "future":
            label_cls += " hour-label--future"

        # peak-value label — только если state == peak
        peak_value_html = (
            f'<span class="peak-value">{format_tokens(v)}</span>'
            if state == "peak" else ""
        )

        cells.append(
            f'<div class="hour-cell" data-hour="{h}">'
            f'{peak_value_html}'
            f'<div class="{cls}" style="height:{h_pct:.1f}%" title="{html.escape(title)}"></div>'
            f'<span class="{label_cls}">{h:02d}</span>'
            f"</div>"
        )

    return (
        f'<div class="chart-shell chart-shell--24h">'
        f'<div class="hours-24h">{"".join(cells)}</div>'
        f"</div>"
    )


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
    # MSK now, расщеплённое на ISO date и hour — прокидывается в JS, чтобы
    # при клике по day-bar корректно решать "is target day today" (будущие
    # часы сегодняшнего дня получают state="future", не "active").
    now_msk_iso = now_msk.date().isoformat()
    now_msk_hour = now_msk.hour
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
    #
    # Inline-зона (шеlvrон + detail-row): рендерится для каждой строки с
    # time_series. Без time_series (0-day проекты) — нет ни шеврона, ни
    # detail-row, как раньше.
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

        # Шеврон: только если у проекта есть time_series. Иначе в первой ячейке
        # только название — никаких лишних контролов над мета-данными.
        if r.time_series is not None:
            chevron = (
                f'<span class="chevron" role="button" tabindex="0" '
                f'aria-expanded="false" aria-controls="detail-{project_esc}">'
                f'▸</span>'
            )
            row_data = f' data-project="{project_esc}"'
        else:
            chevron = ""
            row_data = ""

        body_rows.append(
            f"      <tr{cls}{row_data}>"
            f"<td class=\"project\" data-col=\"project\" data-sort=\"{project_esc}\">"
            f"{chevron}{project_esc}{badge}</td>"
            f"<td class=\"r\" data-col=\"last_update\" data-sort=\"{date_sort}\">{date_esc}</td>"
            f"<td class=\"r\" data-col=\"duration\" data-sort=\"{r.duration_ms}\">{dur_esc}</td>"
            f"<td class=\"r\" data-col=\"tokens\" data-sort=\"{r.tokens}\">{tok_esc}</td>"
            f"<td class=\"r\" data-col=\"rate\" data-sort=\"{rate_raw}\">{rate_esc}</td>"
            f"<td class=\"r\" data-col=\"sessions\" data-sort=\"{r.sessions}\">{sess_esc}</td>"
            f"</tr>"
        )

        # Detail row (скрыт по умолчанию, JS раскрывает по клику на шеврон).
        if r.time_series is not None:
            detail_inner = render_project_detail(
                r.project, r.time_series, r.tokens, now_msk
            )
            body_rows.append(
                f'      <tr class="detail-row" '
                f'id="detail-{project_esc}" '
                f'data-project="{project_esc}" hidden>'
                f'<td colspan="6" class="detail-cell">'
                f'{detail_inner}'
                f'</td></tr>'
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

    /* === Chevron (toggle для inline-зоны) === */
    .chevron {{
      display: inline-block;
      width: 14px;
      margin-right: 8px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
      cursor: pointer;
      user-select: none;
      transition: color 0.15s ease, transform 0.15s ease;
      text-align: center;
    }}
    .chevron:hover {{ color: var(--ink); }}
    .chevron:focus-visible {{
      outline: 1px solid var(--accent);
      outline-offset: 2px;
      border-radius: 2px;
    }}
    .chevron[aria-expanded="true"] {{
      color: var(--ink);
      transform: rotate(90deg);
      display: inline-block;
    }}
    tbody td.project {{ padding-left: 14px; }}

    /* === Detail row (inline-зона) === */
    /* NB: [hidden] даёт display:none из UA stylesheet, но `tr` имеет
       `display: table-row` оттуда же — и table-row выигрывает по
       специфичности, ряд остаётся видимым. Явное правило для скрытия. */
    tbody tr.detail-row[hidden] {{ display: none; }}
    tbody tr.detail-row > td.detail-cell {{
      padding: 0;
      background: var(--panel-2);
      border-bottom: 1px solid var(--line);
    }}
    .detail-inner {{
      padding: 18px 22px 22px;
    }}
    .detail-header {{
      font-size: 11px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 14px;
    }}

    /* === Day chart (дневной ряд) === */
    .day-chart {{
      margin-bottom: 14px;
    }}
    .day-bars {{
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: 1fr;
      gap: 4px;
      height: 96px;
      align-items: end;
    }}
    .day-bar {{
      background: var(--accent-2);
      border-radius: 6px 6px 2px 2px;
      min-height: 2px;
      cursor: pointer;
      transition: filter 0.15s ease, box-shadow 0.15s ease, outline-offset 0.15s ease;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.10);
    }}
    .day-bar:hover {{ filter: brightness(1.18); }}
    .day-bar:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    .day-bar--empty {{
      background: rgba(255, 255, 255, 0.06);
      box-shadow: none;
      cursor: default;
    }}
    .day-bar--empty:hover {{ filter: none; }}
    .day-bar--peak {{
      box-shadow: none;
    }}
    .day-bar--selected {{
      outline: 1px solid rgba(255, 255, 255, 0.55);
      outline-offset: -1px;
    }}
    .day-labels {{
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: 1fr;
      gap: 4px;
      margin-top: 6px;
      text-align: center;
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
    }}
    .day-caption {{
      margin-top: 8px;
      font-size: 11px;
      color: var(--muted);
      text-align: center;
    }}

    /* === Day separator (между daily chart и 24h stream) === */
    .day-separator {{
      font-size: 11px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      padding: 8px 0;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      margin: 6px 0 14px;
    }}

    /* === 24h stream (inline-дубль из build_dashboard.py) === */
    .chart-shell--24h {{
      position: relative;
      padding: 18px 12px 10px;
    }}
    .hours-24h {{
      display: grid;
      grid-template-columns: repeat(24, 1fr);
      gap: 4px;
    }}
    .hour-cell {{
      position: relative;
      display: flex;
      flex-direction: column;
      justify-content: flex-end;
      gap: 6px;
      min-height: 96px;
    }}
    .peak-value {{
      position: absolute;
      top: 4px;
      left: 0;
      right: 0;
      z-index: 1;
      text-align: center;
      color: #e6ebf6;
      font-size: 11px;
      letter-spacing: 0.02em;
      pointer-events: none;
    }}
    .bar-24h {{
      width: 100%;
      flex: 0 1 auto;
      align-self: end;
      border-radius: 6px 6px 2px 2px;
      min-height: 2px;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.10);
      transition: filter 0.15s ease;
    }}
    .bar-24h:hover {{ filter: brightness(1.18); }}
    .bar-24h.active,
    .bar-24h.peak,
    .bar-24h.current {{
      background: #216e39;
    }}
    .bar-24h.peak {{
      box-shadow: none;
    }}
    .bar-24h.current {{
      outline: 1px solid rgba(255, 255, 255, 0.55);
      outline-offset: -1px;
    }}
    .bar-24h.future {{
      height: 6px !important;
      background: transparent;
      border: 1px dashed rgba(255, 255, 255, 0.18);
      box-shadow: none;
      opacity: 0.55;
    }}
    .bar-24h.empty {{
      background: rgba(255, 255, 255, 0.06);
      box-shadow: none;
    }}
    .hour-label {{
      flex: none;
      text-align: center;
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
    }}
    .hour-label--future {{ opacity: 0.4; }}

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
        }} else if (col === "last_update") {{
          // ISO "YYYY-MM-DD" лексикографически совпадает с хронологией.
          // Number("2026-08-07") === NaN — если привести к числу, все строки
          // дают cmp === 0, и сортировка по дате "ломается" (строки остаются
          // в исходном Python-порядке, а индикатор asc/desc врёт).
          cmp = ax < bx ? -1 : ax > bx ? 1 : 0;
        }} else {{
          var an = Number(ax), bn = Number(bx);
          cmp = an < bn ? -1 : an > bn ? 1 : 0;
        }}
        // Tie-breaker: last_update desc (ISO date, строковое сравнение).
        if (cmp === 0) {{
          var aL = sortValueFor(a, "last_update");
          var bL = sortValueFor(b, "last_update");
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

  <script>
    // Server-side timestamp в MSK, нужен JS'у для решения "is target day today"
    // при пересборке 24h после клика по day-bar. Передаётся из билдера
    // (формат ISO date + hour MSK).
    window.__NOW_MSK_ISO__ = "{now_msk_iso}";
    window.__NOW_MSK_HOUR__ = {now_msk_hour};
  </script>

  <script>
    // === Inline-зона: шеврон + day→24h switch ===========================
    //
    // Контракт (рендерится в build_project_dashboard.py):
    //   - <span class="chevron" aria-controls="detail-<slug>">  в первой ячейке
    //   - <tr class="detail-row" id="detail-<slug>" hidden>      сразу после строки
    //   - внутри detail-row: .day-bars / .day-labels / .day-separator /
    //     .hour-chart + <script class="day-hour-map"> + <script class="day-meta">
    //   - now_msk_iso = today MSK date как ISO строка (глобал от билдера)
    //
    // Состояние не persistent: после meta-refresh всё сворачивается.
    // Логика:
    //   - click на chevron → toggle detail-row.hidden + chevron[aria-expanded]
    //   - click на day-bar → JS читает day-hour-map, пересобирает .hour-chart
    //     innerHTML для выбранного дня, обновляет .day-separator, переключает
    //     класс .day-bar--selected на барах.
    (function () {{
      "use strict";
      var NOW_MSK_ISO = window.__NOW_MSK_ISO__ || "";
      var NOW_MSK_HOUR = (typeof window.__NOW_MSK_HOUR__ === "number")
        ? window.__NOW_MSK_HOUR__ : -1;

      function fmtTokens(n) {{
        // Копия Python format_tokens: K=1dp, M=2dp, B=2dp, strip trailing
        // zero + dot. Нужна в JS, чтобы при click'е пересчитать tooltip
        // (хотя фактически tooltip'ы статичные, fmtTokens нужен для
        // атрибутов hour-cell title в пересобранном 24h).
        if (n < 0) return "0";
        if (n < 1000) return String(n);
        if (n < 1e6) {{
          var s = (n / 1e3).toFixed(1);
          if (s.indexOf(".") >= 0) s = s.replace(/0+$/, "").replace(/\\.$/, "");
          return s + "K";
        }}
        if (n < 1e9) {{
          var s2 = (n / 1e6).toFixed(2);
          if (s2.indexOf(".") >= 0) s2 = s2.replace(/0+$/, "").replace(/\\.$/, "");
          return s2 + "M";
        }}
        var s3 = (n / 1e9).toFixed(2);
        if (s3.indexOf(".") >= 0) s3 = s3.replace(/0+$/, "").replace(/\\.$/, "");
        return s3 + "B";
      }}

      function fmtDayShort(iso) {{
        // "YYYY-MM-DD" → "DD MMM" (рус.). Копия Python format_day_short.
        var months = ["янв","фев","мар","апр","май","июн",
                      "июл","авг","сен","окт","ноя","дек"];
        var parts = iso.split("-");
        var d = parseInt(parts[2], 10);
        var m = parseInt(parts[1], 10) - 1;
        return d + " " + months[m];
      }}

      function parseJSONScript(cls) {{
        var el = document.querySelector("." + cls);
        if (!el) return null;
        try {{ return JSON.parse(el.textContent); }}
        catch (e) {{ return null; }}
      }}

      // Сборка 24h cells для конкретного дня. Та же логика, что в
      // Python render_24h_for_day. Возвращает HTML string 24 ячеек.
      function build24hCells(hourMap, targetDay) {{
        var isToday = (targetDay === NOW_MSK_ISO);
        var nowHour = isToday ? NOW_MSK_HOUR : -1;
        var values = [];
        for (var h = 0; h < 24; h++) {{
          values.push(hourMap[String(h)] || 0);
        }}
        // peak: top-1 (>0), наименьший hour
        var peakHour = -1;
        var peakV = 0;
        for (var hh = 0; hh < 24; hh++) {{
          if (values[hh] > peakV) {{ peakV = values[hh]; peakHour = hh; }}
        }}
        // scale_max
        var pastMax = 0;
        for (var h2 = 0; h2 < 24; h2++) {{
          if (!isToday || h2 <= nowHour) {{
            if (values[h2] > pastMax) pastMax = values[h2];
          }}
        }}
        if (pastMax <= 0) pastMax = 1;

        function pct(v) {{
          if (v <= 0 || pastMax <= 0) return 0;
          return Math.max(2.0, Math.min(100.0, (v / pastMax) * 100));
        }}

        var out = [];
        for (var h3 = 0; h3 < 24; h3++) {{
          var v = values[h3];
          var state;
          if (isToday) {{
            if (h3 < nowHour) state = "active";
            else if (h3 === nowHour) state = "current";
            else state = "future";
          }} else {{
            state = "active";
          }}
          if (h3 === peakHour && v > 0) state = "peak";
          if ((state === "active" || state === "current") && v <= 0) state = "empty";

          var cls = "bar-24h " + state;
          var hPct = pct(v);
          var title = (h3 < 10 ? "0" : "") + h3 + ":00–" +
                      (h3 < 10 ? "0" : "") + h3 + ":59: " +
                      (v ? fmtTokens(v) : "нет данных");
          var labelCls = "hour-label" + (state === "future" ? " hour-label--future" : "");
          var peakLabel = state === "peak"
            ? '<span class="peak-value">' + fmtTokens(v) + '</span>'
            : "";
          out.push(
            '<div class="hour-cell" data-hour="' + h3 + '">' +
            peakLabel +
            '<div class="' + cls + '" style="height:' + hPct.toFixed(1) + '%" title="' +
            title.replace(/"/g, "&quot;") + '"></div>' +
            '<span class="' + labelCls + '">' + (h3 < 10 ? "0" : "") + h3 + '</span>' +
            '</div>'
          );
        }}
        return out.join("");
      }}

      // Сборка нового 24h chart-shell для выбранного дня. Полная замена
      // innerHTML у .hour-chart.
      function rebuild24h(detailRow, targetDay) {{
        var map = parseJSONScript("day-hour-map");
        if (!map) return;
        var hourMap = map[targetDay];
        if (!hourMap) return;
        var cells = build24hCells(hourMap, targetDay);
        var hourChart = detailRow.querySelector(".hour-chart");
        if (!hourChart) return;
        hourChart.innerHTML =
          '<div class="chart-shell chart-shell--24h">' +
          '<div class="hours-24h">' + cells + '</div>' +
          '</div>';

        // Обновляем separator
        var sep = detailRow.querySelector(".day-separator");
        if (sep) {{
          var tokens = 0;
          for (var h = 0; h < 24; h++) tokens += (hourMap[String(h)] || 0);
          sep.textContent = fmtDayShort(targetDay) + " · " + fmtTokens(tokens);
          sep.setAttribute("data-selected-day", targetDay);
        }}

        // Переключаем selected на барах
        var bars = detailRow.querySelectorAll(".day-bar");
        for (var i = 0; i < bars.length; i++) {{
          if (bars[i].getAttribute("data-day") === targetDay) {{
            bars[i].classList.add("day-bar--selected");
          }} else {{
            bars[i].classList.remove("day-bar--selected");
          }}
        }}
      }}

      function onChevronClick(ev) {{
        var chev = ev.currentTarget;
        var detailId = chev.getAttribute("aria-controls");
        if (!detailId) return;
        var detail = document.getElementById(detailId);
        if (!detail) return;
        ev.stopPropagation();
        var expanded = chev.getAttribute("aria-expanded") === "true";
        if (expanded) {{
          detail.setAttribute("hidden", "");
          chev.setAttribute("aria-expanded", "false");
        }} else {{
          detail.removeAttribute("hidden");
          chev.setAttribute("aria-expanded", "true");
        }}
      }}

      function onChevronKey(ev) {{
        if (ev.key === "Enter" || ev.key === " ") {{
          ev.preventDefault();
          onChevronClick({{ currentTarget: ev.currentTarget }});
        }}
      }}

      function onDayBarClick(ev) {{
        var bar = ev.currentTarget;
        if (bar.classList.contains("day-bar--empty")) return;
        var day = bar.getAttribute("data-day");
        if (!day) return;
        ev.stopPropagation();
        var detailRow = bar.closest("tr.detail-row");
        if (!detailRow) return;
        rebuild24h(detailRow, day);
      }}

      function onDayBarKey(ev) {{
        if (ev.key === "Enter" || ev.key === " ") {{
          ev.preventDefault();
          onDayBarClick({{ currentTarget: ev.currentTarget }});
        }}
      }}

      function init() {{
        // Event delegation на <tbody>: один handler на parent вместо
        // N штук на каждом шевроне/баре. Устойчиво к meta-refresh и к
        // случаям, когда часть DOM пересоздаётся.
        //
        // NB: ev.target может быть TEXT NODE (напр. символ ▸ внутри
        // <span class="chevron">). closest() есть только на Element,
        // поэтому поднимаемся на parentElement если нужно. Иначе
        // delegation молча не срабатывает — handler не вызывается.
        var tbody = document.querySelector("table tbody");
        if (!tbody) return;
        function resolveEl(t) {{
          if (!t) return null;
          if (t.nodeType === 1) return t;
          if (t.nodeType === 3) return t.parentElement;
          return null;
        }}
        tbody.addEventListener("click", function (ev) {{
          var el = resolveEl(ev.target);
          if (!el || !el.closest) return;
          var chev = el.closest(".chevron");
          if (chev) {{
            onChevronClick({{ currentTarget: chev, stopPropagation: function () {{}} }});
            return;
          }}
          var bar = el.closest(".day-bar");
          if (bar) {{
            onDayBarClick({{ currentTarget: bar, stopPropagation: function () {{}} }});
            return;
          }}
        }});
        // Keydown — отдельно, чтобы не дублировать логику.
        tbody.addEventListener("keydown", function (ev) {{
          if (ev.key !== "Enter" && ev.key !== " ") return;
          var el = resolveEl(ev.target);
          if (!el || !el.closest) return;
          var chev = el.closest(".chevron");
          if (chev) {{
            ev.preventDefault();
            onChevronClick({{ currentTarget: chev, stopPropagation: function () {{}} }});
            return;
          }}
          var bar = el.closest(".day-bar");
          if (bar) {{
            ev.preventDefault();
            onDayBarClick({{ currentTarget: bar, stopPropagation: function () {{}} }});
            return;
          }}
        }});
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
        rows, sid_to_project = collect_projects(con, start_ts_ms, end_ts_ms)
        # Time series per project — отдельная агрегация по token_usage, join
        # через sid_to_project (без повторного SQL на sessions). Для проектов
        # без token_usage в окне time_series=None → в render_html они получают
        # строку без шеврона.
        ts_by_project = collect_time_series(
            con, start_ts_ms, end_ts_ms, sid_to_project
        )
    finally:
        con.close()

    # Прикрепляем time_series к соответствующим ProjectRow. Сборка dict
    # для O(1) lookup, потом итерируем rows. Порядок rows не меняем —
    # сортировку делал collect_projects.
    ts_lookup = {row.project: ts_by_project.get(row.project) for row in rows}
    rows = [
        ProjectRow(
            project=row.project,
            last_update=row.last_update,
            max_ms=row.max_ms,
            duration_ms=row.duration_ms,
            tokens=row.tokens,
            sessions=row.sessions,
            is_active=row.is_active,
            time_series=ts_lookup.get(row.project),
        )
        for row in rows
    ]

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
