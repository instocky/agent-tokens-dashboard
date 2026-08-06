"""Tests for compute_daily_4w / _daily_intensity / DailyBar + daily-section in build_snapshot.

Карточка «Daily · 4 weeks» (calendar heatmap 7×4). Покрывает:
  - state machine (active / current / future / empty) на границах
  - today = Mon / Wed / Sun — разные сочетания past/current/future в W-0
  - 28-дневное выравнивание по ISO-неделям (Пн..Вс × 4 недели, oldest first)
  - intensity-квартили (L1..L4) по 28-дневному распределению
  - current-день участвует в распределении (иначе выпадает из шкалы)
  - empty/future дни в распределение НЕ входят
  - 7d rolling avg + burn_today в build_snapshot["daily"]

Запуск: `python tests/test_daily_4w.py`.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import (  # noqa: E402
    MSK,
    DailyBar,
    WEEK_COUNT,
    WEEKLY_CAP_TOKENS,
    _daily_intensity,
    compute_daily_4w,
)


# ---- helpers ---------------------------------------------------------------


def _monday_of(d: date) -> date:
    """Понедельник ISO-недели для произвольной даты."""
    return d - timedelta(days=d.weekday())


def _hourly_with(daily_totals: dict[date, int]) -> dict[tuple[date, int], int]:
    """Сконвертировать {date: tokens} в dict[(date, hour) -> tokens].

    Каждый день кладём в hour 12 (любой), чтобы has_any=True и
    sum 24h == tokens. Для тестов с частичным днём (current с value=0)
    мы потом руками стираем записи, если нужно.
    """
    return {(d, 12): v for d, v in daily_totals.items()}


# ---- compute_daily_4w: базовая структура ---------------------------------


def test_window_size_is_28() -> None:
    """Окно всегда 4 ISO-недели × 7 дней = 28 DailyBar, oldest first."""
    today = date(2026, 8, 5)  # Ср
    bars = compute_daily_4w({}, today)
    assert len(bars) == 28, f"expected 28, got {len(bars)}"
    # oldest first: первый — понедельник W-3, последний — воскресенье W-0.
    expected_first = _monday_of(today) - timedelta(weeks=WEEK_COUNT - 1)
    expected_last = _monday_of(today) + timedelta(weeks=0) + timedelta(days=6)
    assert bars[0].date == expected_first, f"first={bars[0].date}"
    assert bars[-1].date == expected_last, f"last={bars[-1].date}"


def test_iso_week_alignment() -> None:
    """Строки = Пн..Вс, столбцы = W-3..W-0. weekday 0..6 идёт по возрастанию."""
    today = date(2026, 8, 5)  # Ср
    bars = compute_daily_4w({}, today)
    # Пн..Вс повторяется 4 раза подряд
    expected_weekdays = list(range(7)) * WEEK_COUNT
    actual_weekdays = [b.weekday for b in bars]
    assert actual_weekdays == expected_weekdays, (
        f"weekday pattern broken: {actual_weekdays}"
    )


def test_current_week_flag_only_last_seven() -> None:
    """is_current_week=True только у 7 последних баров (W-0)."""
    today = date(2026, 8, 5)
    bars = compute_daily_4w({}, today)
    flags = [b.is_current_week for b in bars]
    assert flags == [False] * 21 + [True] * 7, f"flags={flags}"


def test_iso_week_field_unique_per_week() -> None:
    """iso_week повторяется 7 раз (4 уникальных номера)."""
    today = date(2026, 8, 5)
    bars = compute_daily_4w({}, today)
    iso_weeks = [b.iso_week for b in bars]
    # 4 группы по 7, каждая группа одного iso_week
    for i in range(WEEK_COUNT):
        group = iso_weeks[i * 7:(i + 1) * 7]
        assert len(set(group)) == 1, f"week {i}: iso_weeks={group}"


# ---- state machine ---------------------------------------------------------


def test_today_monday_one_current_six_future() -> None:
    """today=Пн → 1 current + 6 future в W-0; W-3..W-1 = empty (нет данных)."""
    today = date(2026, 8, 3)  # Пн
    bars = compute_daily_4w({}, today)
    w0 = bars[-7:]
    states = [b.state for b in w0]
    assert states[0] == "current", f"Mon W-0: {states[0]}"
    assert states[1:] == ["future"] * 6, f"Tue..Sun W-0: {states[1:]}"
    # value: current=0, future=None
    assert w0[0].value == 0
    for b in w0[1:]:
        assert b.value is None


def test_today_sunday_all_past_or_current() -> None:
    """today=Вс → Пн..Сб W-0 в прошлом (empty без данных), Вс = current."""
    today = date(2026, 8, 9)  # Вс
    bars = compute_daily_4w({}, today)
    w0 = bars[-7:]
    states = [b.state for b in w0]
    assert "future" not in states, f"no future on Sunday: {states}"
    # Пн..Сб — прошедшие дни без данных → empty; Вс (today) → current.
    assert states == ["empty"] * 6 + ["current"], f"states={states}"


def test_today_wednesday_mixed_w0() -> None:
    """today=Ср → Пн/Вт W-0 пустые (нет данных) или active, Ср=current, Чт..Вс=future."""
    today = date(2026, 8, 5)  # Ср
    # Подкинем данные только в Пн W-0 (несколько часов).
    monday_w0 = today - timedelta(days=2)
    hourly = _hourly_with({monday_w0: 5_000_000})
    bars = compute_daily_4w(hourly, today)
    w0 = bars[-7:]
    states = [b.state for b in w0]
    # Mon=active (5M), Tue=empty (нет данных), Wed=current, Thu..Sun=future
    assert states[0] == "active", f"Mon: {states[0]}"
    assert states[1] == "empty", f"Tue: {states[1]}"
    assert states[2] == "current", f"Wed: {states[2]}"
    assert states[3:] == ["future"] * 4, f"Thu..Sun: {states[3:]}"


def test_empty_day_no_value() -> None:
    """Прошедший день без данных → state=empty, value=None, intensity=None."""
    today = date(2026, 8, 5)  # Ср
    # Никаких данных за Пн W-0 → empty.
    monday_w0 = today - timedelta(days=2)
    hourly = _hourly_with({})  # пусто
    bars = compute_daily_4w(hourly, today)
    monday_bar = next(b for b in bars if b.date == monday_w0)
    assert monday_bar.state == "empty"
    assert monday_bar.value is None
    assert monday_bar.intensity is None


def test_active_day_with_data() -> None:
    """Прошедший день с данными → state=active, value=sum, intensity=L*."""
    today = date(2026, 8, 5)
    monday_w0 = today - timedelta(days=2)
    hourly = _hourly_with({monday_w0: 8_000_000})
    bars = compute_daily_4w(hourly, today)
    monday_bar = next(b for b in bars if b.date == monday_w0)
    assert monday_bar.state == "active"
    assert monday_bar.value == 8_000_000
    # Единственный ненулевой день в окне → n<4 → intensity=L2 (collapse).
    assert monday_bar.intensity == "L2"


# ---- _daily_intensity: квартили -------------------------------------------


def test_intensity_quartiles_8_values() -> None:
    """8 значений → n//4=2, n//2=4, 3n//4=6 → L1..L4 корректно.

    sorted = [1M, 2M, 3M, 4M, 5M, 6M, 7M, 8M].
    q1=sorted[2]=3M, q2=sorted[4]=5M, q3=sorted[6]=7M.
    """
    sorted_active = [1_000_000 * i for i in range(1, 9)]
    cases: list[tuple[int, str]] = [
        (1_000_000, "L1"),
        (3_000_000, "L1"),    # == q1 → L1
        (4_000_000, "L2"),
        (5_000_000, "L2"),    # == q2 → L2
        (6_000_000, "L3"),
        (7_000_000, "L3"),    # == q3 → L3
        (8_000_000, "L4"),
    ]
    for value, expected in cases:
        got = _daily_intensity(value, sorted_active)
        assert got == expected, f"value={value}: got={got}, expected={expected}"


def test_intensity_collapse_to_l2_when_n_lt_4() -> None:
    """n<4 → все non-zero в L2 (как в 24H-карточке)."""
    # 3 значения
    sorted_active = [1_000_000, 5_000_000, 10_000_000]
    for v in sorted_active:
        assert _daily_intensity(v, sorted_active) == "L2", f"v={v}"
    # 1 значение
    assert _daily_intensity(5_000_000, [5_000_000]) == "L2"
    # 3 значения
    assert _daily_intensity(5_000_000, [1_000_000, 5_000_000, 10_000_000]) == "L2"


def test_intensity_none_when_empty_or_zero() -> None:
    """sorted_active=[] → None; value<=0 → None."""
    assert _daily_intensity(5_000_000, []) is None
    assert _daily_intensity(0, [1_000_000, 5_000_000]) is None
    assert _daily_intensity(-1, [1_000_000]) is None


def test_current_day_participates_in_quartile_distribution() -> None:
    """Current-день (today) с value>0 входит в sorted_active, иначе
    он «выпадает» из шкалы (см. ADR §2.3).

    Сценарий: 7 past-дней с value=1M, current=2M.
    sorted = [1M × 7, 2M], n=8, n//4=2, q1=sorted[2]=1M, q2=sorted[4]=1M,
    q3=sorted[6]=1M. value=2M > q3 → L4. Без current 2M-день получил бы
    None (sorted_active=[]), и ячейка на heatmap'е осталась бы без цвета.
    """
    today = date(2026, 8, 9)  # Вс
    past_days = [today - timedelta(days=k) for k in range(1, 8)]  # 7 past дней
    hourly = _hourly_with({d: 1_000_000 for d in past_days})
    # today — current, value=2M.
    hourly[(today, 12)] = 2_000_000
    bars = compute_daily_4w(hourly, today)
    today_bar = next(b for b in bars if b.date == today)
    assert today_bar.state == "current"
    assert today_bar.value == 2_000_000
    # 8 значений (7×1M + 2M), все 1M ≤ q3, 2M > q3 → L4.
    assert today_bar.intensity == "L4", f"current={today_bar.intensity}"
    # И контр-сценарий: убрать current из распределения — было бы None.
    # (Это документирующее утверждение, негативный путь отдельно не тестим.)


def test_empty_and_future_excluded_from_quartiles() -> None:
    """Future/empty дни НЕ входят в sorted_active.

    Сценарий: 1 active=1M, 1 empty (нет данных), 1 future.
    sorted_active = [1M] (n=1 < 4 → L2). active получает L2, не None.
    """
    today = date(2026, 8, 5)  # Ср
    # Пн W-0: active с 1M. Вт W-0: empty. Ср=current. Чт=future.
    monday_w0 = today - timedelta(days=2)
    hourly = _hourly_with({monday_w0: 1_000_000})
    bars = compute_daily_4w(hourly, today)
    monday_bar = next(b for b in bars if b.date == monday_w0)
    assert monday_bar.state == "active"
    assert monday_bar.intensity == "L2", "n=1 → L2"
    # Ни одного None в intensity у будущих/пустых
    future_empty = [b for b in bars if b.state in ("future", "empty")]
    for b in future_empty:
        assert b.intensity is None, f"{b.state} {b.date}: intensity={b.intensity}"


def test_quartile_split_28_days_with_current() -> None:
    """Реалистичный сценарий: 28 дней с разными значениями, current участвует.

    4 недели × 7 дней = 28 значений; сегодня = Ср W-0 (idx 17 в окне).
    Покрытие: 28 значений 1M..28M; current=20M, остальные распределены.
    """
    today = date(2026, 8, 5)  # Ср
    monday_w3 = _monday_of(today) - timedelta(weeks=3)
    daily_totals: dict[date, int] = {}
    for i in range(28):
        d = monday_w3 + timedelta(days=i)
        if d == today:
            daily_totals[d] = 20_000_000  # current
        elif d > today:
            pass  # future — без hourly
        else:
            daily_totals[d] = (i + 1) * 1_000_000  # 1M..27M
    hourly = _hourly_with(daily_totals)
    bars = compute_daily_4w(hourly, today)
    # current-day (Ср W-0) → state=current, value=20M, intensity есть.
    today_bar = next(b for b in bars if b.date == today)
    assert today_bar.state == "current"
    assert today_bar.value == 20_000_000
    assert today_bar.intensity in ("L1", "L2", "L3", "L4")
    # Все future — value=None, intensity=None.
    for b in bars:
        if b.state == "future":
            assert b.value is None
            assert b.intensity is None


# ---- build_snapshot["daily"] -----------------------------------------------


def _make_dummy_db() -> Path:
    """Создать in-memory SQLite с парой строк за разные дни.

    Нужен для build_snapshot (открывает БД через URI). Используем
    tmp-файл, потому что open_db требует file-URI.
    """
    import sqlite3
    import tempfile

    tmp = tempfile.NamedTemporaryFile(
        prefix="test_daily_", suffix=".sqlite", delete=False
    )
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.execute(
        "CREATE TABLE local_runtime_token_usage ("
        "ts INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
        "session_id TEXT, agent_name TEXT, model TEXT"
        ")"
    )
    con.execute(
        "CREATE TABLE local_runtime_message_rows ("
        "created_at_ms INTEGER, session_id TEXT, role TEXT, turn_id INTEGER"
        ")"
    )
    con.execute(
        "CREATE TABLE local_runtime_sessions ("
        "session_id TEXT PRIMARY KEY, record_json TEXT"
        ")"
    )
    con.commit()
    con.close()
    return Path(tmp.name)


def test_build_snapshot_daily_section_shape() -> None:
    """build_snapshot возвращает секцию daily со всеми ожидаемыми полями."""
    db_path = _make_dummy_db()
    try:
        now_msk = datetime(2026, 8, 5, 12, 0, 0, tzinfo=MSK)
        # Импортируем лениво, чтобы тест не зависел от порядка sys.path.
        from analytics import build_snapshot

        snap = build_snapshot(db_path, now_msk=now_msk)
        assert "daily" in snap, "нет секции daily"
        daily = snap["daily"]
        assert set(daily.keys()) == {
            "since", "weeks", "current_weekday", "weekly_cap",
            "burn_today", "burn_7d_avg",
        }, f"daily keys: {set(daily.keys())}"
        assert daily["weekly_cap"] == WEEKLY_CAP_TOKENS
        assert daily["current_weekday"] == 2  # Ср = 2 (Пн=0)
        assert isinstance(daily["weeks"], list)
        assert len(daily["weeks"]) == 28
        # Все weeks — DailyBar.
        for b in daily["weeks"]:
            assert isinstance(b, DailyBar), f"unexpected type {type(b)}"
        # since = понедельник W-3.
        expected_since = _monday_of(now_msk.date()) - timedelta(weeks=3)
        assert daily["since"] == expected_since
        # Пустая БД → все дни (кроме today=Ср) либо empty, либо future;
        # 7d_avg == 0 → burn_7d_avg=None, burn_today="none".
        assert daily["burn_7d_avg"] is None
        assert daily["burn_today"] == "none"
    finally:
        # Windows: sqlite3 connections могут держать файл до GC.
        import gc
        gc.collect()
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass


def test_build_snapshot_daily_burn_with_real_data() -> None:
    """burn_7d_avg и burn_today считаются по реальным данным."""
    import sqlite3

    db_path = _make_dummy_db()
    try:
        con = sqlite3.connect(db_path)
        today = date(2026, 8, 5)  # Ср
        # Сегодня (Ср) = 1M (current).
        today_noon = datetime.combine(today, datetime.min.time(), tzinfo=MSK)
        today_noon = today_noon.replace(hour=12)
        today_ms = int(today_noon.timestamp() * 1000)
        con.execute(
            "INSERT INTO local_runtime_token_usage VALUES (?, ?, ?, ?, ?, ?)",
            (today_ms, 500_000, 500_000, "s1", "test", "m"),
        )
        # 6 предыдущих дней, каждый по 5M (в hour=12 каждого дня).
        for k in range(1, 7):
            d = today - timedelta(days=k)
            dt = datetime.combine(d, datetime.min.time(), tzinfo=MSK).replace(hour=12)
            ms = int(dt.timestamp() * 1000)
            con.execute(
                "INSERT INTO local_runtime_token_usage VALUES (?, ?, ?, ?, ?, ?)",
                (ms, 2_500_000, 2_500_000, "s1", "test", "m"),
            )
        con.commit()
        con.close()

        from analytics import build_snapshot

        snap = build_snapshot(
            db_path, now_msk=datetime(2026, 8, 5, 12, 0, 0, tzinfo=MSK)
        )
        daily = snap["daily"]
        # 7d window = [today-6 .. today] — 7 дней, 6 past × 5M + 1 current × 1M.
        # sum = 31_000_000, avg = 31_000_000 // 7 = 4_428_571.
        assert daily["burn_7d_avg"] is not None
        assert daily["burn_7d_avg"] == 4_428_571, f"7d_avg={daily['burn_7d_avg']}"
        # today_value=1M, 7d_avg=4.43M → 1/4.43 = 0.226 → "ok" (< 0.8).
        assert daily["burn_today"] == "ok", f"burn_today={daily['burn_today']}"
    finally:
        import gc
        gc.collect()
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---- main ------------------------------------------------------------------


def main() -> int:
    tests = [
        test_window_size_is_28,
        test_iso_week_alignment,
        test_current_week_flag_only_last_seven,
        test_iso_week_field_unique_per_week,
        test_today_monday_one_current_six_future,
        test_today_sunday_all_past_or_current,
        test_today_wednesday_mixed_w0,
        test_empty_day_no_value,
        test_active_day_with_data,
        test_intensity_quartiles_8_values,
        test_intensity_collapse_to_l2_when_n_lt_4,
        test_intensity_none_when_empty_or_zero,
        test_current_day_participates_in_quartile_distribution,
        test_empty_and_future_excluded_from_quartiles,
        test_quartile_split_28_days_with_current,
        test_build_snapshot_daily_section_shape,
        test_build_snapshot_daily_burn_with_real_data,
    ]
    passed = 0
    failed: list[str] = []
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}\n    {e}")
            failed.append(t.__name__)
    print(f"\n{passed}/{len(tests)} tests passed")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
