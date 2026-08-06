"""build_analytics.py — static daily-analytics dashboard generator (entry).

Читает `local_runtime_token_usage` из runtime-state.sqlite, собирает
self-contained `analytics.html` — calendar heatmap 4 недели × 7 дней
(второй вид, см. ADR-002). Никакого backend, никакого внешнего JSON.

Спецификация: adr-0806-daily-analytics.md (ADR-002 v1.0)
Запуск:  python build_analytics.py
Опции:   --db <path>     путь к sqlite (по умолчанию DB_PATH ниже)
         --out <path>    путь к выходному HTML (по умолчанию OUTPUT_PATH ниже)
         --no-write      не записывать файл (dry-run, печатает в stdout)
         --quiet         не печатать лог

ADR-001 v2.1 + ADR-002 §2.1: модуль содержит ТОЛЬКО entry (CLI + main).
Compute, DB-доступ, форматирование и snapshot-builder живут в analytics.py;
рендеринг HTML/CSS — в render_analytics.py. Граница: build_analytics
НЕ содержит бизнес-логики (правило 3 §2.2 ADR-001), main читает поля из
snapshot и пишет HTML одной строкой.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# ---- DB / output paths ------------------------------------------------------

# Абсолютные пути по умолчанию — рядом со скриптом. Переопределяются --db/--out.
# DB_PATH совпадает с build_dashboard.DB_PATH: оба entry читают одну и ту же БД.
DB_PATH: Path = Path("C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite")
OUTPUT_PATH: Path = Path(__file__).resolve().parent / "analytics.html"


# ---- CLI -------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Сгенерировать self-contained analytics.html (calendar heatmap 4 недели) из runtime-state.sqlite.",
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


# ---- main ------------------------------------------------------------------

def main() -> int:
    import analytics
    import render_analytics

    args = parse_args()

    # Прогрессия логов в UTF-8 (Windows-консоль).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    log = (lambda *a, **kw: print(*a, **kw)) if not args.quiet else (lambda *a, **kw: None)

    log(f"[build] db  = {args.db}")
    log(f"[build] out = {args.out}")

    # Сборка snapshot: analytics открывает БД сама, делает все SQL-запросы и
    # считает derived-поля (daily.burn_today, daily.burn_7d_avg). main() никакой
    # бизнес-логики не делает — только читает поля snapshot'а и пишет лог
    # (правило 3 §2.2 ADR-001).
    snapshot = analytics.build_snapshot(args.db, now_msk=datetime.now(analytics.MSK))

    now_msk = snapshot["now_msk"]
    daily = snapshot["daily"]

    log(f"[build] today (MSK) = {now_msk.date()}  "
        f"ISO W-{now_msk.isocalendar()[1]}  weekday={now_msk.isocalendar()[2]}")
    log(f"[build] daily.since = {daily['since']} (Monday of W-{daily['since'].isocalendar()[1]})")
    log(f"[build] daily.weeks = {len(daily['weeks'])} days  "
        f"current_weekday={daily['current_weekday']}")
    log(f"[build] daily.burn_today = {daily['burn_today']}  "
        f"burn_7d_avg = {daily['burn_7d_avg']}")
    # Распределение по состояниям — короткая sanity-сводка для лога.
    from collections import Counter
    state_counts = Counter(b.state for b in daily["weeks"])
    log(f"[build] daily states: " + ", ".join(
        f"{k}={v}" for k, v in sorted(state_counts.items())
    ))

    # Единственная строка бизнес-логики в entry: свести snapshot в HTML.
    html = render_analytics.render(snapshot)

    if args.no_write:
        sys.stdout.write(html)
        log("\n[build] --no-write: stdout-only, файл не тронут")
    else:
        args.out.write_text(html, encoding="utf-8")
        log(f"[build] wrote {args.out} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
