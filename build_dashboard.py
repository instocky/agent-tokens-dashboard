"""build_dashboard.py — static token-usage dashboard generator (entry).

Читает `local_runtime_token_usage` из runtime-state.sqlite, собирает
self-contained `dashboard.html` (без backend, без внешнего JSON).

Спецификация: prd-token-dashboard-prototype.md
Запуск:  python build_dashboard.py
Опции:   --db <path>     путь к sqlite (по умолчанию DB_PATH ниже)
         --out <path>    путь к выходному HTML (по умолчанию OUTPUT_PATH ниже)
         --no-write      не записывать файл (dry-run, печатает в stdout)
         --quiet         не печатать лог

Расписание: Windows Task Scheduler, раз в 5 минут.

ADR-001 v2.1 (см. adr-0806.md): модуль содержит ТОЛЬКО entry (CLI + main).
Compute, DB-доступ, форматирование и snapshot-builder живут в analytics.py;
рендеринг HTML/CSS/JS — в render_dashboard.py. Граница: build_dashboard
НЕ содержит бизнес-логики (правило 3 §2.2 ADR), main читает поля из
snapshot и пишет HTML одной строкой.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# ---- DB / output paths ------------------------------------------------------

# Абсолютные пути по умолчанию — рядом со скриптом. Переопределяются --db/--out.
DB_PATH: Path = Path("C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite")
OUTPUT_PATH: Path = Path(__file__).resolve().parent / "dashboard.html"


# ---- CLI -------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Сгенерировать self-contained dashboard.html из runtime-state.sqlite.",
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
    import render_dashboard

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
    # считает derived-поля (weekly threshold, day_level, session.level).
    # main() никакой бизнес-логики не делает — только читает поля snapshot'а
    # и пишет лог (правило 3 §2.2 ADR).
    snapshot = analytics.build_snapshot(args.db, now_msk=datetime.now(analytics.MSK))

    now_msk = snapshot["now_msk"]
    weekly = snapshot["weekly"]
    today = snapshot["today"]
    window = snapshot["window"]
    session = snapshot["session"]

    log(f"[build] today (MSK) = {now_msk.date()}  "
        f"ISO W-{now_msk.isocalendar()[1]}  weekday={now_msk.isocalendar()[2]}")
    log(f"[build] since = {weekly['since']} (Monday of W-{weekly['since'].isocalendar()[1]})")
    log(
        f"[build] weekly_cap={analytics.WEEKLY_CAP_TOKENS}  "
        f"today_spent={weekly['today_spent']}  days_left={weekly['days_left']}  "
        f"threshold={weekly['threshold']}"
    )
    log(
        f"[build] current_session  sid={session['id']}  "
        f"tokens={session['tokens']}  requests={session['requests']}  "
        f"path={session['path']}  project={session['project_title']}  "
        f"session_title={session['record_title']}  "
        f"duration={analytics.fmt_duration(session['duration_ms'])}"
    )
    log(f"[build] active_window = {window['label']}, total = {window['total']}")
    for w in weekly["weeks"]:
        days_repr = ",".join(
            analytics.fmt_int(v) if v is not None else "—"
            for v in w.days
        )
        log(f"[build]   {w.label} ({'current' if w.is_current else 'past  '})  [{days_repr}]")
    if today["peak_24h"] is not None:
        ph, pv = today["peak_24h"]
        log(f"[build] today_24h peak = {ph:02d}:00 ({pv} tokens)")
    else:
        log(f"[build] today_24h peak = none (no past hours with data)")

    # Единственная строка бизнес-логики в entry: свести snapshot в HTML.
    # Вся графика, CSS, JS, threshold-расчёты, форматирование — внутри
    # render_dashboard.render.
    html = render_dashboard.render(snapshot)

    if args.no_write:
        sys.stdout.write(html)
        log("\n[build] --no-write: stdout-only, файл не тронут")
    else:
        args.out.write_text(html, encoding="utf-8")
        log(f"[build] wrote {args.out} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
