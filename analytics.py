"""analytics.py — Database Access + Business Logic + Formatting + Snapshot Builder.

Выделено из build_dashboard.py в рамках ADR-001 v2.1 (разделение на три слоя:
analytics / render / entry). Содержит:

  - Constants & Domain Types
  - Database Access (SQLite + извлечение сырых данных)
  - Business Logic (compute_* + pill_level + project_title_from_path)
  - Formatting Helpers (fmt_*)
  - Snapshot Builder (build_snapshot → dict по контракту §2.4 ADR)

См. также:
  - adr-0806.md — обоснование, правила слоёв, контракт snapshot.
  - build_dashboard.py — entry + render.
  - render_dashboard.py — появится в Step 2.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ------------------------------------------------------------------
# Constants & Domain Types
# ------------------------------------------------------------------

# Europe/Moscow = UTC+3 круглый год (с 2014 без перехода на зимнее время).
# Хардкод константой, как и просили — без zoneinfo-зависимостей.
MSK = timezone(timedelta(hours=3))

# 5-часовые слоты (полу-открытые [start, end)). 4 дневных по 5 часов + 1 ночной
# 4 часа (23:00 вчера → 03:00 сегодня, переход через полночь). PRD §6.3.
# Лейблы — half-open: "03:00–08:00" значит [3, 8), т.е. часы 3,4,5,6,7.
# Ночной слот асимметричен (4h) — сознательно, не добиваем до 5h через 22 или 3.
WINDOWS: list[dict] = [
    {"name": "morning",   "hours": [3, 4, 5, 6, 7],     "label": "03:00–08:00", "wraps": False},
    {"name": "midday",    "hours": [8, 9, 10, 11, 12],  "label": "08:00–13:00", "wraps": False},
    {"name": "afternoon", "hours": [13, 14, 15, 16, 17], "label": "13:00–18:00", "wraps": False},
    {"name": "evening",   "hours": [18, 19, 20, 21, 22], "label": "18:00–23:00", "wraps": False},
    {"name": "night",     "hours": [23, 0, 1, 2],       "label": "23:00–03:00", "wraps": True},
]
NIGHT_SLOT = WINDOWS[4]
WEEK_COUNT: int = 4                                # PRD §6.4
WEEKDAY_LABELS: tuple[str, ...] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# Недельная квота (cap) на input+output токены. На текущий день weekly chart
# выводится красная пунктирная полоска с подписью — это «потолок» сегодняшнего
# расхода, ниже которого нужно остаться, чтобы уложиться в капу за 7 дней
# (включая сегодня). Если сегодня превысил — порог автоматически пересчитается
# на завтра (формула зависит от today_spent и days_left, оба обновляются).
WEEKLY_CAP_TOKENS: int = 60_000_000

# CSS-имена для каждого уровня (используются в разметке как .bar-24h.intensity-N
# и .bar-24h.peak). Семантические state'ы (active/peak/current/future/empty)
# добавляются как дополнительные классы — НЕ вместо intensity.
_HOUR_STATE_LEVELS: tuple[str, ...] = ("L1", "L2", "L3", "L4")

# Ключ в record_json таблицы local_runtime_sessions, по которому достаём
# путь workspace текущей сессии. Источник истины — runtime v2, см. анализ
# inspect_session.py (2026-08-05): поле workspaceDir присутствует в record_json.
_SESSION_RECORD_PATH_KEY = "workspaceDir"

# Ключ в record_json для названия сессии (например, "TB07 Idempotency Photos").
# Семантически ОТЛИЧАЕТСЯ от workspaceDir: title — это имя ветки/работы,
# выставляемое пользователем при старте сессии (в UI runtime'а), а workspaceDir —
# папка репозитория. На pill'е показываем оба через разделитель • (см. обсуждение
# 2026-08-05 в review к session-pill'у: "если хочется title — добавь отдельным
# элементом через •").
_SESSION_RECORD_TITLE_KEY = "title"


@dataclass(frozen=True)
class Week:
    """Одна неделя для grouped-bar chart."""
    label: str          # "W-32"
    monday: date        # понедельник этой недели (MSK)
    days: list[int | None]   # 7 значений Пн..Вс, None = disabled/no data
    is_current: bool


@dataclass(frozen=True)
class HourlyBar:
    """Один час карточки «TODAY · 24H STREAM».

    `state` — семантический статус (что рисовать):
      "active"  — h < now.hour и value > 0
      "current" — h == now.hour (in-progress, всё ещё копит)
      "peak"    — top-1 по value среди всех ACTIVE+CURRENT (включая current)
      "future"  — h > now.hour (ещё не наступил)
      "empty"   — h < now.hour и value == 0 (час прошёл, данных нет)
    `intensity` — уровень палитры (L1..L4) для active/current/peak.
      Для future/empty = None (рендер сам выбирает класс).
    """
    hour: int           # 0..23
    value: int          # токены за этот час (>= 0)
    state: str
    intensity: str | None  # "L1" | "L2" | "L3" | "L4" | None


@dataclass(frozen=True)
class DailyBar:
    """Один день в 4-недельной daily-view (calendar heatmap).

    Поля:
      date           — MSK-дата дня.
      value          — сумма за 24 часа дня. None = empty (прошедший день без
                       данных) или future (дата ещё не наступила).
      state          — "active" (прошедший день с данными) | "current"
                       (сегодня, копит) | "future" (ещё не наступил) | "empty"
                       (прошедший день без данных).
      intensity      — L1..L4 для active/current с value > 0, иначе None.
                       Квартили считаются по всем ненулевым дням окна
                       (включая current — иначе сегодня «выпадает» из шкалы).
      weekday        — 0..6 (Пн..Вс, date.weekday()).
      iso_week       — ISO-номер недели (1..53).
      is_current_week — True, если этот день принадлежит последней из 4 недель
                        окна (W-0).
    """
    date: date
    value: int | None
    state: str
    intensity: str | None  # "L1" | "L2" | "L3" | "L4" | None
    weekday: int         # 0..6 (Пн..Вс)
    iso_week: int
    is_current_week: bool


# ------------------------------------------------------------------
# Database Access
# ------------------------------------------------------------------


def open_db(path: Path) -> sqlite3.Connection:
    """Открыть SQLite в режиме read-only через URI.

    mode=ro гарантирует, что мы не сможем случайно писать в базу
    и не заблокируем писателей runtime'а.
    """
    if not path.exists():
        raise FileNotFoundError(f"SQLite не найден: {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    return con


def aggregate_by_hour(con: sqlite3.Connection, since_msk_date: date) -> dict[tuple[date, int], int]:
    """SELECT (date, hour, sum_tokens) GROUP BY date,hour, начиная с since_msk_date (MSK).

    Возвращает {(msk_date, msk_hour): tokens}, где tokens = input + output.
    Cache/reasoning/cost намеренно исключены (PRD §5).
    """
    since_msk_midnight = datetime.combine(since_msk_date, datetime.min.time(), tzinfo=MSK)
    since_ts_ms = int(since_msk_midnight.timestamp() * 1000)

    # Преобразование ms→MSK делаем в SQLite: '+3 hours' — детерминированно,
    # не зависит от локали машины. strftime('%H') → строка, кастуем в INT.
    sql = """
        SELECT
            date(ts / 1000, 'unixepoch', '+3 hours')                          AS msk_date,
            CAST(strftime('%H', ts / 1000, 'unixepoch', '+3 hours') AS INT)  AS msk_hour,
            COALESCE(SUM(input_tokens + output_tokens), 0)                    AS tokens
        FROM local_runtime_token_usage
        WHERE ts >= ?
        GROUP BY msk_date, msk_hour
    """
    out: dict[tuple[date, int], int] = {}
    for d_str, h, tokens in con.execute(sql, (since_ts_ms,)):
        out[(date.fromisoformat(d_str), int(h))] = int(tokens)
    return out


def compute_today_meta(con: sqlite3.Connection, now_msk: datetime) -> tuple[int, int, float]:
    """Meta-метрики карточки «Сегодня» из `local_runtime_message_rows`.

    Возвращает кортеж (sessions, user_messages, avg_requests_per_session)
    за период [00:00 MSK сегодня, now_msk].

    Контракт по составу:
      - `sessions` — `COUNT(DISTINCT session_id)` среди строк, попавших в окно.
        Сюда попадает любая сессия с ЛЮБЫМ событием (user/assistant/tool/None)
        после полуночи MSK — то же определение, что и в token_usage-based
        варианте ("сессия, в которой сегодня что-то происходило").
      - `user_messages` — `COUNT(*) WHERE role='user'`. Только реальные вопросы
        пользователя; assistant/tool/None-роли НЕ считаются. Это сознательно:
        см. обсуждение 2026-08-04 — turn_id в token_usage неотличим от
        internal-loop'ов, user-role в message_rows — единственный надёжный
        сигнал "юзер что-то спросил".
      - `avg` = user_messages / sessions (float). При sessions == 0 → 0.0.

    Edge cases:
      - Никаких строк в окне: (0, 0, 0.0).
      - Все роли == None или != 'user': sessions>0, user_messages=0, avg=0.0.

    Performance: full scan по таблице за сегодня. На текущих объёмах (<10K
    строк) незаметно. Беклог: миграция `CREATE INDEX ... ON
    local_runtime_message_rows(created_at_ms)` для будущего роста.
    """
    today = now_msk.date()
    since_msk_midnight = datetime.combine(today, datetime.min.time(), tzinfo=MSK)
    since_ts_ms = int(since_msk_midnight.timestamp() * 1000)

    sessions_row = con.execute(
        "SELECT COUNT(DISTINCT session_id) FROM local_runtime_message_rows "
        "WHERE created_at_ms >= ?",
        (since_ts_ms,),
    ).fetchone()
    user_msgs_row = con.execute(
        "SELECT COUNT(*) FROM local_runtime_message_rows "
        "WHERE role = 'user' AND created_at_ms >= ?",
        (since_ts_ms,),
    ).fetchone()

    sessions = int(sessions_row[0]) if sessions_row else 0
    user_messages = int(user_msgs_row[0]) if user_msgs_row else 0
    avg = (user_messages / sessions) if sessions > 0 else 0.0
    return sessions, user_messages, avg


def compute_current_session(
    con: sqlite3.Connection, now_msk: datetime
) -> tuple[str | None, int, int, str | None, int | None]:
    """Метрики самой свежей сессии за сегодня (для hero-pill "session").

    Возвращает (session_id, tokens, user_requests, path, duration_ms) для сессии
    с самым поздним событием в окне [00:00 MSK, now_msk]:
      - tokens, user_requests — для pill'а формата `actual(requests) / avg(...)`.
        Числитель и знаменатель в одних единицах (per-session), иначе
        today_tokens / avg_tokens_per_session = today_sessions (тривиально = N
        сессий) и pill всегда красный — бесполезный сигнал.
      - path — workspaceDir из local_runtime_sessions.record_json (см.
        _fetch_session_path). None если таблицы/поля нет или путь пустой.
      - duration_ms — MAX(created_at_ms) − MIN(created_at_ms) по ВСЕЙ сессии
        (без фильтра по дню). Даже если текущее сообщение пришло сегодня в 17:26,
        а сессия стартовала вчера в 23:10 — длительность покроет весь хвост.

    Логика:
      1. Находим session_id с MAX(created_at_ms) за сегодня (LIMIT 1).
      2. Суммируем input+output по token_usage для этой сессии за сегодня.
      3. Считаем user-сообщения для этой сессии за сегодня.
      4. Достаём workspaceDir из local_runtime_sessions.record_json.
      5. Считаем MIN/MAX по created_at_ms всей сессии.

    Edge cases:
      - Нет строк за сегодня → (None, 0, 0, None, None); рендер покажет "—".
      - Самая свежая сессия есть, но в token_usage для неё 0 строк →
        (sid, 0, K, path, dur). Pill покажет actual=0 → level="none".
      - local_runtime_sessions отсутствует (старые runtime, тесты без этой
        таблицы) → (sid, tokens, requests, None, dur); _fetch_session_path
        глушит sqlite3.OperationalError.

    Performance: 5 запросов (3 по message_rows, 1 по token_usage, 1 по sessions).
    Каждый full-scan / PK-lookup. На <10K строках незаметно (<10 мс).
    Беклог — индекс по created_at_ms.
    """
    today = now_msk.date()
    since_msk_midnight = datetime.combine(today, datetime.min.time(), tzinfo=MSK)
    since_ts_ms = int(since_msk_midnight.timestamp() * 1000)

    sid_row = con.execute(
        "SELECT session_id FROM local_runtime_message_rows "
        "WHERE created_at_ms >= ? "
        "ORDER BY created_at_ms DESC LIMIT 1",
        (since_ts_ms,),
    ).fetchone()
    if sid_row is None or sid_row[0] is None:
        return None, 0, 0, None, None
    session_id = str(sid_row[0])

    tok_row = con.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) "
        "FROM local_runtime_token_usage "
        "WHERE session_id = ? AND ts >= ?",
        (session_id, since_ts_ms),
    ).fetchone()
    tokens = int(tok_row[0]) if tok_row else 0

    req_row = con.execute(
        "SELECT COUNT(*) FROM local_runtime_message_rows "
        "WHERE session_id = ? AND role = 'user' AND created_at_ms >= ?",
        (session_id, since_ts_ms),
    ).fetchone()
    user_requests = int(req_row[0]) if req_row else 0

    path = _fetch_session_path(con, session_id)

    dur_row = con.execute(
        "SELECT MIN(created_at_ms), MAX(created_at_ms) "
        "FROM local_runtime_message_rows "
        "WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    duration_ms: int | None = None
    if dur_row and dur_row[0] is not None and dur_row[1] is not None:
        duration_ms = int(dur_row[1]) - int(dur_row[0])

    return session_id, tokens, user_requests, path, duration_ms


def _fetch_session_path(con: sqlite3.Connection, session_id: str) -> str | None:
    """Путь workspace из record_json в local_runtime_sessions.

    Семантика:
      - SELECT record_json WHERE session_id=? — одна строка на сессию.
      - json.loads(record_json) — record_json это строка-JSON в SQLite.
      - record[_SESSION_RECORD_PATH_KEY] — 'workspaceDir' в runtime v2
        (подтверждено inspect_session.py 2026-08-05).

    Failure modes (все → None, без exceptions наружу):
      - Таблица local_runtime_sessions отсутствует (старые runtime, тесты
        без неё) → sqlite3.OperationalError → None.
      - session_id не найден → fetchone() вернёт None.
      - record_json битый / None → JSONDecodeError / TypeError → None.
      - workspaceDir пустой или не строка → None.

    Безопасна для частого вызова: один SELECT, один json.loads, никаких
    побочных эффектов.
    """
    try:
        row = con.execute(
            "SELECT record_json FROM local_runtime_sessions "
            "WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        record = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    path = record.get(_SESSION_RECORD_PATH_KEY)
    return str(path) if path else None


def _fetch_session_title(con: sqlite3.Connection, session_id: str | None) -> str | None:
    """Название сессии (ветки/работы) из record_json в local_runtime_sessions.

    Семантически ОТЛИЧАЕТСЯ от `_fetch_session_path` (workspaceDir):
      - workspaceDir = путь к репозиторию/папке ("C:/Projects/Python/0803_...").
      - title = имя, которое пользователь дал сессии ("TB07 Idempotency Photos").

    В record_json v2 поле title может быть:
      - строкой (нормальный случай, приходит из UI runtime при старте сессии);
      - пустой строкой (пользователь не задал имя) — трактуем как None, чтобы
        на pill'е не висел пустой блок;
      - отсутствовать (старые runtime, тесты без поля) — None.

    Failure modes (все → None, без exceptions наружу — параллельно с
    `_fetch_session_path`):
      - session_id=None (compute_current_session вернул None) → None без
        обращения к БД. Защищает от лишнего SELECT'а в случае пустого дня.
      - Таблица local_runtime_sessions отсутствует → sqlite3.OperationalError → None.
      - session_id не найден → fetchone() вернёт None.
      - record_json битый / None → JSONDecodeError / TypeError → None.
      - title пустой или не строка → None.

    Безопасна для частого вызова: один SELECT, один json.loads, никаких
    побочных эффектов.
    """
    if not session_id:
        return None
    try:
        row = con.execute(
            "SELECT record_json FROM local_runtime_sessions "
            "WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        record = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    title = record.get(_SESSION_RECORD_TITLE_KEY)
    if not isinstance(title, str):
        return None
    stripped = title.strip()
    return stripped if stripped else None


# ------------------------------------------------------------------
# Business Logic
# ------------------------------------------------------------------


def compute_current_hour(hourly: dict[tuple[date, int], int], today: date) -> int:
    """Токены за текущий календарный час (MSK). 0 если данных нет."""
    now_msk = datetime.now(MSK)
    return hourly.get((today, now_msk.hour), 0)


def compute_today(hourly: dict[tuple[date, int], int], now_msk: datetime) -> int:
    """Токены с начала суток (00:00) до текущего часа включительно (MSK).

    Running total: включает in-progress current_hour, поэтому значение
    "допрыгивает" в течение последнего часа. Это сознательно — карточка
    "Токены / сегодня" по своей природе дубль части графика окна (по
    запросу TL: "просится карточка, даже если это дубль графика").
    """
    today = now_msk.date()
    return sum(hourly.get((today, h), 0) for h in range(now_msk.hour + 1))


def current_window(now_msk: datetime) -> dict:
    """Какой 5h-слот активен сейчас (MSK).

    Дневные слоты [3, 8), [8, 13), [13, 18), [18, 23) — half-open: старший час
    входит в предыдущий слот. Ночной слот [23, 3) — оборачивает полночь
    (23:00 одного дня + 0..2:00 следующего).
    """
    h = now_msk.hour
    if h >= 23 or h < 3:
        return NIGHT_SLOT
    idx = (h - 3) // 5  # 0..3
    return WINDOWS[idx]


def compute_current_window(
    hourly: dict[tuple[date, int], int], now_msk: datetime
) -> tuple[int, list[tuple[int, int, date]], str]:
    """Сумма + per-hour (с датами) + лейбл для активного слота.

    Для ночного слота час 23 берётся из (today - 1 day), часы 0..2 — из today.
    Для остальных слотов все часы — из today.
    """
    today = now_msk.date()
    window = current_window(now_msk)
    entries: list[tuple[int, int, date]] = []
    if window["wraps"]:
        yesterday = today - timedelta(days=1)
        for h in window["hours"]:
            d = yesterday if h == 23 else today
            entries.append((h, hourly.get((d, h), 0), d))
    else:
        for h in window["hours"]:
            entries.append((h, hourly.get((today, h), 0), today))
    total = sum(v for _, v, _ in entries)
    return total, entries, window["label"]


def compute_weekly(
    hourly: dict[tuple[date, int], int], today: date, week_count: int = WEEK_COUNT
) -> list[Week]:
    """Последние `week_count` недель, oldest-first.

    Логика disabled-баров (None):
      - day_date > today           → None (будущее, данных быть не может)
      - day_date < today и count=0 → None (прошедший день, но логов нет — нет данных)
      - day_date == today          → реальная сумма (может быть 0, "только начался")
    """
    iso = today.isocalendar()  # (iso_year, iso_week, iso_weekday 1..7)
    current_monday = today - timedelta(days=iso[2] - 1)

    weeks: list[Week] = []
    for i in range(week_count):
        # weeks[0] = самая старая, weeks[-1] = текущая
        offset = week_count - 1 - i
        monday = current_monday - timedelta(weeks=offset)
        label = f"W-{monday.isocalendar()[1]}"
        is_current = (monday == current_monday)

        days: list[int | None] = []
        for d_idx in range(7):
            day_date = monday + timedelta(days=d_idx)
            if day_date > today:
                # Будущий день (любой недели) — данных быть не может
                days.append(None)
                continue
            # Прошедший день или сегодня: если ни одной строки в БД за этот день
            # (любой час) — None (no data yet), иначе реальная сумма.
            has_any = any((day_date, h) in hourly for h in range(24))
            if not has_any:
                days.append(None)
                continue
            days.append(sum(hourly.get((day_date, h), 0) for h in range(24)))
        weeks.append(Week(label=label, monday=monday, days=days, is_current=is_current))
    return weeks


def compute_weekly_threshold(
    weekly_cap: int, today_spent: int, days_left: int
) -> int | None:
    """«Потолок» расхода на сегодня (накопительно), чтобы уложиться в weekly_cap.

    Формула:  threshold = max(0, (cap − today_spent) / days_left)  (с floor).

    Семантика:
      - threshold — это максимум, который можно потратить СЕГОДНЯ (с начала суток
        до конца дня), чтобы при равномерном расходе на оставшиеся дни общая
        сумма за неделю не превысила `weekly_cap`.
      - Если сегодня уже потратил больше, чем threshold, — завтра формула
        пересчитается (today_spent станет больше, days_left меньше → новый
        порог). Это и есть «если превысил — на следующий день уровень
        пересчитается».
      - days_left включает сегодня:  Пн=7, Вт=6, …, Вс=1. Считается как
        `8 − isoweekday(today)`.

    Возвращает:
      - int ≥ 0 — сам threshold (clamped в 0 снизу для консервативности).
      - None   — если `days_left <= 0` (нечего считать; защита от деления на 0).
                 На UI такие случаи маловероятны (текущий день всегда ≥ 1),
                 но контракт это явно фиксирует.

    Параметры намеренно плоские (без `now_msk`/SQLite) — функция чистая,
    тестируется без моков. Вызов из main() подставляет реальные числа.
    """
    if days_left <= 0:
        return None
    remaining = weekly_cap - today_spent
    if remaining <= 0:
        # Вся капа уже исчерпана (или превышена) — сегодня больше тратить не надо.
        return 0
    # floor вниз: лучше показать чуть заниженный порог, чем подтолкнуть к
    # превышению. 10.71M → 10M, не 11M.
    return remaining // days_left


def _intensity_level(value: int, sorted_active: list[int]) -> str:
    """Уровень GitHub-палитры (L1..L4) по квартилю среди ненулевых ACTIVE-часов.

    Семантика:
      - sorted_active — ненулевые значения часов, отсортированные по возрастанию.
      - L1 — нижний квартиль, L4 — верхний. Границы считаются по позиции в
        отсортированном массиве (len // 4, len // 2, 3 * len // 4).
      - Если sorted_active пуст или len < 4 — все значения попадают в L2
        (визуально нейтральный «средний» уровень, не прыгает по шкале на
        единственном баре — это не «победитель», а просто «есть данные»).
      - Возвращает уровень палитры (одна из констант _HOUR_STATE_LEVELS).
    """
    n = len(sorted_active)
    if n == 0 or value <= 0:
        return "L2"  # fallback; для active'ов сюда не попадаем (value > 0)
    if n < 4:
        # Мало данных — не плодим квартили, всё в L2. Это сознательно:
        # один бар с 5M и второй с 6M не должны иметь визуальный разрыв L1 vs L4.
        return "L2"
    # Границы квартилей: позиции, не значения. Ранжируем value через bisect.
    import bisect
    q1 = sorted_active[n // 4]
    q2 = sorted_active[n // 2]
    q3 = sorted_active[(3 * n) // 4]
    if value <= q1:
        return "L1"
    if value <= q2:
        return "L2"
    if value <= q3:
        return "L3"
    return "L4"


def compute_today_24h(
    hourly: dict[tuple[date, int], int], now_msk: datetime
) -> list[HourlyBar]:
    """24-часовая разбивка сегодняшнего дня (MSK) для карточки «24H STREAM».

    Возвращает список из 24 HourlyBar (hour=0..23) с уже размеченными state
    и intensity. Порядок: по возрастанию hour.

    Правила:
      - peak — единственный час с максимальным value среди (h <= now.hour).
        Если в эти часы value=0 везде — peak'а нет (state никогда не "peak").
      - current — ровно h == now.hour (даже если value=0). Если current оказывается
        одновременно максимумом — он же и peak (state="peak" + "current" в рендере
        объединяются; см. _render_24h_stream).
      - intensity назначается ТОЛЬКО ненулевым active/current/peak; для peak
        intensity не используется (отдельный класс .bar-24h.peak).
    """
    today = now_msk.date()
    now_h = now_msk.hour

    # 1. Сырые значения по часам.
    raw: list[tuple[int, int]] = [(h, hourly.get((today, h), 0)) for h in range(24)]

    # 2. Находим peak: максимум среди h <= now_h. Если все нули — peak'а нет.
    past = [(h, v) for h, v in raw if h <= now_h]
    peak_hour: int | None = None
    peak_val: int = 0
    for h, v in past:
        if v > peak_val:
            peak_val = v
            peak_hour = h

    # 3. Сортируем ненулевые past-значения для квартилей.
    sorted_active = sorted(v for _, v in past if v > 0)

    bars: list[HourlyBar] = []
    for h, v in raw:
        if h > now_h:
            state = "future"
            intensity = None
        elif h == now_h:
            # current — помечаем peak'ом, если он же оказался максимумом
            state = "peak" if peak_hour == h else "current"
            intensity = _intensity_level(v, sorted_active) if v > 0 else None
        elif v == 0:
            state = "empty"
            intensity = None
        else:
            state = "peak" if peak_hour == h else "active"
            intensity = _intensity_level(v, sorted_active)
        bars.append(HourlyBar(hour=h, value=v, state=state, intensity=intensity))
    return bars


def today_24h_peak(bars: list[HourlyBar]) -> tuple[int, int] | None:
    """Удобный хелпер: (peak_hour, peak_value) или None, если peak'а нет."""
    for b in bars:
        if b.state == "peak":
            return (b.hour, b.value)
    return None


def _daily_intensity(value: int, sorted_active: list[int]) -> str | None:
    """Уровень GitHub-палитры (L1..L4) для одного дня в 4-недельном heatmap'е.

    Семантика (аналог `_intensity_level`, но на дневной сетке):
      - sorted_active — ненулевые значения ДНЕЙ, отсортированные по возрастанию.
        Сюда включаются и current-дни (сегодня), иначе текущий день «выпадает»
        из шкалы, и его ячейка на heatmap'е не имеет цвета.
      - Границы квартилей — по позиции: n//4, n//2, 3n//4 (как в 24H-карточке).
      - Если sorted_active пуст или n < 4 — все значения получают L2 (визуально
        нейтральный средний уровень; на малом N нет смысла в четырёхступенчатой
        шкале — см. ADR §2.3).
      - value <= 0 → None (для current с value=0 день только начался, шкала
        бессмысленна; для future/empty value=None по построению — здесь их нет).

    Чистая функция: тестируется без моков.
    """
    n = len(sorted_active)
    if n == 0 or value <= 0:
        return None
    if n < 4:
        return "L2"
    q1 = sorted_active[n // 4]
    q2 = sorted_active[n // 2]
    q3 = sorted_active[(3 * n) // 4]
    if value <= q1:
        return "L1"
    if value <= q2:
        return "L2"
    if value <= q3:
        return "L3"
    return "L4"


def compute_daily_4w(
    hourly: dict[tuple[date, int], int],
    today: date,
    week_count: int = WEEK_COUNT,
) -> list[DailyBar]:
    """4-недельная daily-view (calendar heatmap), oldest-first.

    Возвращает список из week_count * 7 = 28 DailyBar (фиксировано):
    Пн..Вс × (W-3, W-2, W-1, W-0), oldest → newest. Колонка W-0 = текущая
    неделя. Соседство с `compute_weekly`: то же окно, те же дни — отличается
    плотностью (heatmap vs grouped-bars) и наличием intensity/state.

    Логика состояний (взаимоисключающие):
      - "future"  — day_date > today (любой недели). value=None, intensity=None.
      - "current" — day_date == today. value = sum 24h (может быть 0).
                    intensity = `_daily_intensity(value, sorted)` при value > 0,
                    иначе None.
      - "empty"   — day_date < today, и за день нет ни одной строки в `hourly`
                    (все 24 часа отсутствуют). value=None, intensity=None.
      - "active"  — day_date < today, и за день есть хотя бы одна строка.
                    value = sum 24h, intensity = `_daily_intensity(value, sorted)`.

    Квартили считаются ОДИН РАЗ для всего окна по ненулевым дням
    (active + current-with-value>0), иначе сегодня «выпадает» из шкалы —
    см. ADR-002 §2.3. Future/empty в распределение не входят (value=None).

    Edge cases:
      - today = Пн → 1 current (Пн W-0) + 6 future (Вт..Вс W-0) + 21 active/empty.
        Окно всё равно 28.
      - today = Вс → 7 active/current (вся W-0 в прошлом) + 0 future.
        Окно всё равно 28.
      - Вся БД пустая (hourly == {}) → 7 current с value=0 (если today в W-0),
        21 empty (W-3..W-1). Quartile base пустой → все intensity = None.

    Параметры плоские (без now_msk/SQLite) — функция чистая, тестируется
    без моков. Вызов из build_snapshot подставляет `hourly` и `today` из
    уже агрегированных данных.
    """
    iso = today.isocalendar()  # (iso_year, iso_week, iso_weekday 1..7)
    current_monday = today - timedelta(days=iso[2] - 1)
    since = current_monday - timedelta(weeks=week_count - 1)

    # Проход 1: собираем 28 daily-баров без intensity, чтобы посчитать sorted_active.
    raw: list[DailyBar] = []
    for w_idx in range(week_count):
        # w_idx=0 → самая старая неделя, w_idx=week_count-1 → текущая.
        monday = since + timedelta(weeks=w_idx)
        is_current_week = (monday == current_monday)
        for d_idx in range(7):
            day_date = monday + timedelta(days=d_idx)
            if day_date > today:
                state = "future"
                value: int | None = None
            elif day_date == today:
                value = sum(hourly.get((day_date, h), 0) for h in range(24))
                state = "current"
            else:
                has_any = any((day_date, h) in hourly for h in range(24))
                if not has_any:
                    state = "empty"
                    value = None
                else:
                    value = sum(hourly.get((day_date, h), 0) for h in range(24))
                    state = "active"
            raw.append(
                DailyBar(
                    date=day_date,
                    value=value,
                    state=state,
                    intensity=None,  # заполним в проходе 2
                    weekday=day_date.weekday(),
                    iso_week=day_date.isocalendar()[1],
                    is_current_week=is_current_week,
                )
            )

    # Квартили считаются ОДИН РАЗ для всего окна (28 дней).
    sorted_active = sorted(
        b.value for b in raw if b.value is not None and b.value > 0
    )

    # Проход 2: назначаем intensity по квартилям.
    out: list[DailyBar] = []
    for b in raw:
        if b.value is not None and b.value > 0:
            intensity = _daily_intensity(b.value, sorted_active)
        else:
            intensity = None
        out.append(
            DailyBar(
                date=b.date,
                value=b.value,
                state=b.state,
                intensity=intensity,
                weekday=b.weekday,
                iso_week=b.iso_week,
                is_current_week=b.is_current_week,
            )
        )
    return out


def project_title_from_path(path: str | None) -> str | None:
    """Извлекает короткое имя проекта из workspace-пути.

    Берёт последний сегмент пути и снимает префикс вида 'NNNN_' / 'NNNN-'
    (наш конвенциональный маркер даты/порядка папок: '0731_college-publisher'
    → 'college-publisher', '0803_agent-tokens-dashboard' → 'agent-tokens-dashboard').

    Edge cases:
      - None / пустая строка → None (рендер покажет '—').
      - Только один сегмент (например, 'project') → 'project' (префикса нет).
      - После strip пусто (например, '0803_') → возвращаем оригинальный
        сегмент, чтобы не терять информацию (на UI будет '0803_', но это
        лучше, чем пустота).

    Нормализация: backslash → slash, trailing separators отрезаем — на
    Windows путь может прийти с '\\' на конце, на POSIX с '/'.
    """
    if not path:
        return None
    normalized = path.rstrip("/\\").replace("\\", "/")
    segments = [s for s in normalized.split("/") if s]
    if not segments:
        return None
    last = segments[-1]
    stripped = re.sub(r"^\d+[_\-]+", "", last)
    return stripped if stripped else last


def pill_level(actual: int | None, cap: int | None) -> str:
    """Цветовой уровень для числителя hero-pill (actual / cap).

    Контракт (обсуждение 2026-08-05, TL review):
      - "ok"   — actual ≤ 80% от cap (зелёный).
      - "warn" — 80% < actual ≤ 100% (оранжевый, порог приближается).
      - "over" — actual > 100% (красный, превышение).
      - "none" — actual/cap отсутствуют или cap <= 0 (нейтральный,
                 рендерим числитель как обычный текст без подсветки).
                 Возвращается "none" и при actual==0 (день ещё не начался):
                 нечего подсвечивать, нейтральный цвет честнее зелёного.

    Чистая функция: тестируется без моков, как и `compute_weekly_threshold`.

    Контракт вызова (ADR §2.2 rule 8 + §2.3): зовётся ТОЛЬКО из build_snapshot
    для `session.level` и `weekly.day_level`. Step 1 оставляет её доступной для
    render-стороны (`_build_hero_pill_inner` в build_dashboard.py) как
    документированное транзитное исключение; Step 2 уберёт импорт и
    пробросит level как параметр.
    """
    if actual is None or cap is None or cap <= 0 or actual <= 0:
        return "none"
    pct = actual / cap
    if pct > 1.0:
        return "over"
    if pct > 0.8:
        return "warn"
    return "ok"


# ------------------------------------------------------------------
# Formatting Helpers
# ------------------------------------------------------------------


def fmt_tokens(n: int | None) -> str:
    """182500 → '182.5K', 1234567 → '1.23M', None → '—'."""
    if n is None:
        return "—"
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K"
    return f"{n / 1_000_000:.2f}M"


def fmt_log_tick(n: int) -> str:
    """Log-axis tick label: '1M' / '10M' / '100M' / '100K' (без .0 / .00).

    На log-оси визуальное положение тика уже передаёт порядок, так что
    trailing decimals избыточны (и на стыке порядков могут выглядеть как
    дробные значения). Применяется ТОЛЬКО к подписям осей, не к KPI
    (где .1K / .2M — нужная precision).
    """
    s = fmt_tokens(n)
    if s.endswith("K") or s.endswith("M"):
        num, suffix = s[:-1], s[-1]
        # "100.0" → "100" → "100K";  "1.00" → "1" → "1M";  "5.50" → "5.5" → "5.5M"
        return num.rstrip("0").rstrip(".") + suffix
    return s


def fmt_avg(value: float) -> str:
    """Среднее число запросов на сессию: 3.5 → '3.5', 2.0 → '2', 4.04 → '4.0'.

    Контракт (обсуждение 2026-08-04):
      - Всегда один знак после запятой, КРОМЕ целых значений (2, 3, 5, …) —
        для них '.0' визуальный шум. На 2.7 будет '2.7', на 2.0 будет '2'.
      - Отрицательные не ожидаются (avg = user_msgs / sessions ≥ 0). Защита
        для value < 0: ведём себя так же, как для value >= 0 (один знак, .0
        убираем у целых). Это сознательно — fallback'и типа '0' или '—' тут
        не нужны, вызывающий код знает контекст.
      - Используется ТОЛЬКО в meta-строке карточки «Сегодня» для avg.
    """
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def fmt_int(n: int | None) -> str:
    """Без K/M, для осей и лейблов."""
    return "—" if n is None else f"{n:,}".replace(",", " ")


def fmt_duration(ms: int | None) -> str:
    """Длительность сессии в читаемом виде.

    Контракт:
      - < 0 или None → '—' (нейтральный fallback).
      - < 60s         → 'Ns' (например, первый запрос в сессии: '12s').
      - < 60min       → 'Nmin' (например, '40min').
      - < 1h и m=0    → 'Nh' (например, '2h').
      - иначе         → 'Nh Mmin' (например, '1h 20min').

    Используется в context-pill'е рядом с session/day pills. Мин/масштаб
    выбраны под кейс "TL смотрит на дашборд раз в N минут и хочет быстро
    понять, как давно открыта текущая сессия".
    """
    if ms is None or ms < 0:
        return "—"
    total_s = ms // 1000
    if total_s < 60:
        return f"{total_s} s"
    total_min = total_s // 60
    if total_min < 60:
        return f"{total_min} min"
    h, m = divmod(total_min, 60)
    return f"{h} h" if m == 0 else f"{h} h {m} min"


# ------------------------------------------------------------------
# Snapshot Builder
# ------------------------------------------------------------------


def build_snapshot(db_path: Path, now_msk: datetime | None = None) -> dict:
    """Собрать snapshot по контракту §2.4 ADR.

    Возвращает dict с шестью секциями: hour / today / window / weekly /
    session / daily. Каждая секция — отдельный локальный dict, чтобы контракт
    не сводился к плоскому литералу на 100 строк (см. §2.4).

    Параметр `now_msk` опционален: если None, берётся `datetime.now(MSK)`.
    Это позволяет тестам и demo_24h.py фиксировать «сейчас» детерминированно.
    Сама функция открывает БД сама (read-only URI), выполняет все нужные
    SQL-запросы и собирает данные.

    В Step 1 — callable, но main() в build_dashboard.py ещё НЕ использует
    build_snapshot (Step 2). Сейчас main() повторяет ту же логику построчно;
    переезд будет тривиальным (подставить snapshot, читать поля).
    """
    if now_msk is None:
        now_msk = datetime.now(MSK)
    today = now_msk.date()

    # 4-недельное окно: с понедельника 3 недели назад от текущего.
    iso = today.isocalendar()
    current_monday = today - timedelta(days=iso[2] - 1)
    since = current_monday - timedelta(weeks=WEEK_COUNT - 1)

    with open_db(db_path) as con:
        hourly = aggregate_by_hour(con, since)
        today_sessions, today_user_requests, today_avg = compute_today_meta(con, now_msk)
        (
            _current_sid,
            current_session_tokens,
            current_session_requests,
            current_session_path,
            current_session_duration_ms,
        ) = compute_current_session(con, now_msk)
        current_session_record_title = _fetch_session_title(con, _current_sid)

    current_session_title = project_title_from_path(current_session_path)

    current_hour_tokens = compute_current_hour(hourly, today)
    today_tokens = compute_today(hourly, now_msk)
    # Среднее число токенов (input+output) на одну сессию за сегодня.
    # None при today_sessions==0 — чтобы pill показал "—", а не "0/сессию"
    # (sessions считаются из message_rows и независимы от token_usage, см.
    # обсуждение compute_today_meta про «сессия, в которой что-то происходило»).
    # int-truncation: today_tokens / today_sessions — для pill-формата дробная
    # часть не нужна, fmt_tokens сам подберёт K/M.
    avg_tokens_per_session: int | None = (
        int(today_tokens / today_sessions) if today_sessions > 0 else None
    )
    window_total, window_entries, window_label = compute_current_window(hourly, now_msk)
    window_wraps = current_window(now_msk)["wraps"]
    weeks = compute_weekly(hourly, today)

    today_24h_bars = compute_today_24h(hourly, now_msk)
    today_24h_peak_val = today_24h_peak(today_24h_bars)

    # Daily 4-week view (heatmap). Считается из того же `hourly` — никакого
    # нового SQL. compute_daily_4w возвращает 28 DailyBar (Пн..Вс × 4 недели),
    # выровненных по ISO-неделям, что важно для day-of-week pattern
    # (строки = Пн..Вс, столбцы = W-3..W-0).
    daily_bars = compute_daily_4w(hourly, today)

    # burn_7d_avg: среднее токенов/день за последние 7 дней (today + 6 prev).
    # По построению 7d_window не содержит future-дней (все даты <= today).
    # empty-дни (value=None) вносят 0; current-день (today) вносит свой
    # running total (даже если 0). При all-zero → None (сигнал "недостаточно
    # данных для сравнения" — pill покажет "—"). Floor-деление, как в
    # `compute_weekly_threshold` (консервативнее для пороговых сравнений).
    daily_by_date: dict[date, int | None] = {b.date: b.value for b in daily_bars}
    burn_7d_window = [today - timedelta(days=k) for k in range(6, -1, -1)]
    burn_7d_sum = sum((daily_by_date.get(d) or 0) for d in burn_7d_window)
    burn_7d_avg: int | None = burn_7d_sum // 7 if burn_7d_sum > 0 else None

    # burn_today: pill_level(today_value, 7d_avg) при 7d_avg > 0, иначе "none".
    # Семантика: "сегодня расходуется vs средний день последних 7 дней".
    today_value = daily_by_date.get(today) or 0
    if burn_7d_avg is not None and burn_7d_avg > 0:
        burn_today = pill_level(today_value, burn_7d_avg)
    else:
        burn_today = "none"

    # Порог расхода на сегодня (weekly cap threshold). Считаем только если
    # текущая неделя действительно последняя в окне (она всегда последняя по
    # логике compute_weekly) и для today есть ненулевая запись. Если записи
    # ещё нет — today_spent=0, threshold=cap/days_left (нормальный кейс для
    # самого начала дня).
    current_week = weeks[-1]
    today_idx = now_msk.weekday()  # 0=Пн..6=Вс
    today_spent = current_week.days[today_idx] or 0
    days_left = 8 - now_msk.isoweekday()  # Пн=7, Вс=1
    threshold = compute_weekly_threshold(WEEKLY_CAP_TOKENS, today_spent, days_left)

    hour = {
        "tokens": current_hour_tokens,
    }

    today_dict = {
        "tokens": today_tokens,
        "sessions": today_sessions,
        "user_requests": today_user_requests,
        "avg": today_avg,
        "avg_tokens_per_session": avg_tokens_per_session,
        "bars_24h": today_24h_bars,
        "peak_24h": today_24h_peak_val,
    }

    window_dict = {
        "total": window_total,
        "entries": window_entries,
        "label": window_label,
        "wraps": window_wraps,
    }

    weekly_dict = {
        "since": since,
        "weeks": weeks,
        "cap": WEEKLY_CAP_TOKENS,
        "today_spent": today_spent,
        "days_left": days_left,
        "threshold": threshold,
        "day_level": pill_level(today_spent, threshold),
    }

    session_dict = {
        "id": _current_sid,
        "tokens": current_session_tokens,
        "requests": current_session_requests,
        "path": current_session_path,
        "project_title": current_session_title,
        "record_title": current_session_record_title,
        "duration_ms": current_session_duration_ms,
        "level": pill_level(current_session_tokens, WEEKLY_CAP_TOKENS),
    }

    daily_dict = {
        "since": since,                          # понедельник W-3 (старт окна 4 недель)
        "weeks": daily_bars,                     # 28 DailyBar (Пн..Вс × 4 недели)
        "current_weekday": now_msk.weekday(),    # 0..6 (Пн..Вс)
        "weekly_cap": WEEKLY_CAP_TOKENS,         # для burn-rate context, не для pill
        "burn_today": burn_today,                # "ok" | "warn" | "over" | "none"
        "burn_7d_avg": burn_7d_avg,              # None если 7d_avg == 0
    }

    return {
        "now_msk": now_msk,
        "hour": hour,
        "today": today_dict,
        "window": window_dict,
        "weekly": weekly_dict,
        "session": session_dict,
        "daily": daily_dict,
    }
