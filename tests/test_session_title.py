"""Tests for `_fetch_session_title` (record_json.title из local_runtime_sessions).

Источник истины — runtime v2, см. `_SESSION_RECORD_TITLE_KEY = "title"`. Семантика
отличается от `_fetch_session_path` (workspaceDir): title — это имя ветки/работы,
выставленное пользователем в UI runtime'а, а workspaceDir — папка репозитория.

Покрывает:
  - happy path: строка из record_json.title возвращается as-is.
  - whitespace-only ("  ") трактуется как пусто (None) — пустой блок на pill'е
    хуже, чем fallback.
  - пустая строка "" → None.
  - title отсутствует (record_json без поля) → None.
  - title не строка (None / int / list) → None — record_json это свободный
    JSON из runtime, типизация не гарантирована.
  - session_id=None → None без обращения к БД (compute_current_session может
    вернуть None для пустого дня — не делаем лишний SELECT).
  - session_id не найден в таблице → None.
  - таблица local_runtime_sessions отсутствует → None (старые runtime,
    параллельно с `_fetch_session_path`).
  - record_json битый → None, исключение не утекает наружу.
  - record_json=None в БД → None.

Запуск: `python tests/test_session_title.py`.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import _fetch_session_title  # noqa: E402


# ---- helpers ---------------------------------------------------------------


def _make_db_with_session(record_json_str: str | None) -> sqlite3.Connection:
    """In-memory SQLite c одной строкой в local_runtime_sessions.

    `record_json_str` кладётся как есть (включая None → NULL).
    """
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE local_runtime_sessions ("
        "  session_id TEXT PRIMARY KEY,"
        "  record_json TEXT"
        ")"
    )
    con.execute(
        "INSERT INTO local_runtime_sessions (session_id, record_json) VALUES (?, ?)",
        ("mvs_test_session", record_json_str),
    )
    con.commit()
    return con


# ---- happy path -----------------------------------------------------------


def test_returns_title_when_present() -> None:
    """Строка из record_json.title возвращается as-is."""
    rec = json.dumps({"workspaceDir": "C:/x", "title": "TB07 Idempotency Photos"})
    con = _make_db_with_session(rec)
    try:
        assert _fetch_session_title(con, "mvs_test_session") == "TB07 Idempotency Photos"
    finally:
        con.close()


def test_strips_whitespace_around_title() -> None:
    """Strip вокруг title — runtime иногда пишет ' name ' с пробелами, они
    не нужны на UI; для пустого результата после strip → None (см. ниже).
    """
    rec = json.dumps({"title": "  Padded Name  "})
    con = _make_db_with_session(rec)
    try:
        assert _fetch_session_title(con, "mvs_test_session") == "Padded Name"
    finally:
        con.close()


# ---- empty / missing / wrong type ------------------------------------------


def test_whitespace_only_title_returns_none() -> None:
    """Whitespace-only title ('  ') → None. Иначе на pill'е висит пустой блок
    с разделителем '•' и смотрится как '• project' (визуальный мусор).
    """
    rec = json.dumps({"title": "   "})
    con = _make_db_with_session(rec)
    try:
        assert _fetch_session_title(con, "mvs_test_session") is None
    finally:
        con.close()


def test_empty_string_title_returns_none() -> None:
    """Пустая строка → None (аналогично whitespace-only)."""
    rec = json.dumps({"title": ""})
    con = _make_db_with_session(rec)
    try:
        assert _fetch_session_title(con, "mvs_test_session") is None
    finally:
        con.close()


def test_missing_title_field_returns_none() -> None:
    """record_json без поля title → None. Старые runtime не пишут title,
    новая схема несовместима со старыми записями — graceful degradation."""
    rec = json.dumps({"workspaceDir": "C:/x"})  # title отсутствует
    con = _make_db_with_session(rec)
    try:
        assert _fetch_session_title(con, "mvs_test_session") is None
    finally:
        con.close()


def test_title_is_none_returns_none() -> None:
    """record_json.title = null (валидный JSON) → None, не TypeError."""
    rec = json.dumps({"title": None})
    con = _make_db_with_session(rec)
    try:
        assert _fetch_session_title(con, "mvs_test_session") is None
    finally:
        con.close()


def test_title_is_int_returns_none() -> None:
    """record_json.title = 42 (теоретически возможно, runtime — свободный
    JSON) → None. Защита от нестрокового title; html.escape в pill'е ждёт str.
    """
    rec = json.dumps({"title": 42})
    con = _make_db_with_session(rec)
    try:
        assert _fetch_session_title(con, "mvs_test_session") is None
    finally:
        con.close()


def test_title_is_list_returns_none() -> None:
    """record_json.title = [...] (массив) → None, не list-as-string на UI."""
    rec = json.dumps({"title": ["a", "b"]})
    con = _make_db_with_session(rec)
    try:
        assert _fetch_session_title(con, "mvs_test_session") is None
    finally:
        con.close()


# ---- session_id handling --------------------------------------------------


def test_session_id_none_returns_none_without_db_hit() -> None:
    """session_id=None → None БЕЗ SELECT'а.

    compute_current_session возвращает None для пустого дня (не начался).
    Хелпер не должен лезть в БД в этом случае — день пустой, сессии нет.
    Коннект вообще не открывается, поэтому тест не требует фикстуры.
    """
    # Используем реальный con, но session_id=None; ожидаем None + 0 SELECT'ов.
    con = _make_db_with_session(json.dumps({"title": "X"}))
    try:
        # При session_id=None хелдер делает return до SELECT'а. Проверяем
        # функционально: результат None. Проверка «SELECT не было» здесь
        # не строгая (мы не считаем query'и), но и с DB всё равно None.
        assert _fetch_session_title(con, None) is None
    finally:
        con.close()


def test_session_id_empty_string_returns_none() -> None:
    """session_id='' → None. Защита от edge case'а из runtime."""
    con = _make_db_with_session(json.dumps({"title": "X"}))
    try:
        assert _fetch_session_title(con, "") is None
    finally:
        con.close()


def test_session_id_not_found_returns_none() -> None:
    """session_id есть, но в таблице нет строки с ним → None."""
    con = _make_db_with_session(json.dumps({"title": "X"}))
    try:
        assert _fetch_session_title(con, "mvs_unknown") is None
    finally:
        con.close()


# ---- failure modes (no exceptions) ----------------------------------------


def test_missing_table_returns_none() -> None:
    """Таблица local_runtime_sessions отсутствует → None, OperationalError наружу не уходит.

    Параллельный сценарий с `_fetch_session_path`: старые runtime или
    in-memory коннект в тестах без этой таблицы не должны валить build.
    """
    con = sqlite3.connect(":memory:")
    # таблицу НЕ создаём
    try:
        assert _fetch_session_title(con, "any") is None
    finally:
        con.close()


def test_broken_json_returns_none() -> None:
    """record_json битый (не парсится) → None, JSONDecodeError наружу не уходит."""
    con = _make_db_with_session("{ this is not json")
    try:
        assert _fetch_session_title(con, "mvs_test_session") is None
    finally:
        con.close()


def test_null_record_json_returns_none() -> None:
    """record_json IS NULL → None, TypeError наружу не уходит."""
    con = _make_db_with_session(None)
    try:
        assert _fetch_session_title(con, "mvs_test_session") is None
    finally:
        con.close()


# ---- runner ----------------------------------------------------------------


def main() -> int:
    tests = [
        # happy path
        test_returns_title_when_present,
        test_strips_whitespace_around_title,
        # empty / missing / wrong type
        test_whitespace_only_title_returns_none,
        test_empty_string_title_returns_none,
        test_missing_title_field_returns_none,
        test_title_is_none_returns_none,
        test_title_is_int_returns_none,
        test_title_is_list_returns_none,
        # session_id handling
        test_session_id_none_returns_none_without_db_hit,
        test_session_id_empty_string_returns_none,
        test_session_id_not_found_returns_none,
        # failure modes
        test_missing_table_returns_none,
        test_broken_json_returns_none,
        test_null_record_json_returns_none,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} tests passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
