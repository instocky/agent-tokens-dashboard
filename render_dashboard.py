"""render_dashboard.py — render(snapshot) -> HTML-строка (ADR-001 v2.1 §2.2 rule 8).

Чистая render-сторона. Принимает snapshot (dict по контракту §2.4 ADR),
собранный `analytics.build_snapshot(db_path, now_msk)`. Никогда не открывает
SQLite и не зовёт compute-функции напрямую — все данные приходят через
snapshot.

Импортирует из analytics только константы, доменные типы и форматирующие
хелперы (`fmt_*`) — направление импорта однонаправленное render → analytics
(правило 8 §2.2 ADR). Compute, доступ к БД и `pill_level` живут в
analytics; уровень pill'а (`weekly.day_level` / `session.level`)
вычисляется в `build_snapshot` и пробрасывается сюда параметром.

Запуск напрямую не предполагается — это модуль, импортируемый из
`build_dashboard.py` (entry) и `demo_24h.py`.
"""
from __future__ import annotations

import html
import math
from datetime import datetime, timedelta

# Импорты из analytics — допустимые по §2.2 rule 8 ADR (константы, типы,
# форматтеры). НЕ импортируем compute-функции, DB-доступ и `pill_level`.
from analytics import (  # noqa: E402
    WEEKDAY_LABELS,
    WEEKLY_CAP_TOKENS,
    HourlyBar,
    Week,
    fmt_avg,
    fmt_duration,
    fmt_int,
    fmt_log_tick,
    fmt_tokens,
)

GITHUB_PALETTE: dict[str, str] = {
    "L0":     "#ebedf0",
    "L1":     "#9be9a8",
    "L2":     "#40c463",
    "L3":     "#30a14e",
    "L4":     "#216e39",
    "PEAK":   "#00d97e",  # ярче максимального L4, чтобы визуально отделиться
}

# ---- CLI -------------------------------------------------------------------

# ---- HTML rendering --------------------------------------------------------


def _bar_height_pct(value: int, scale: str, y_info) -> float:
    """Высота бара в % (2..100). 0 → 0 (рендерится как min-height floor)."""
    if value <= 0:
        return 0.0
    if scale == "log" and y_info is not None:
        y_min, y_max, _ = y_info
        if y_min <= 0 or y_max <= 0 or y_max <= y_min:
            return 0.0
        log_min = math.log10(y_min)
        log_max = math.log10(y_max)
        frac = (math.log10(value) - log_min) / (log_max - log_min)
        return max(2.0, min(100.0, frac * 100))
    if scale == "linear" and isinstance(y_info, int) and y_info > 0:
        frac = value / y_info
        return max(2.0, min(100.0, frac * 100))
    return 0.0


# ---- HTML rendering --------------------------------------------------------

# Текущая max-высота для шкалы weekly chart. Считаем от реальных значений,
# округляя вверх до удобного тика.
def _y_max_for(weeks: list[Week]) -> int:
    """Linear: max шкалы, округлённый вверх до удобного тика."""
    values = [d for w in weeks for d in w.days if d is not None]
    if not values:
        return 1000
    # Округляем max до ближайших 100K, минимум 200K.
    raw = max(values)
    if raw < 200_000:
        return 200_000
    step = 200_000 if raw <= 2_000_000 else 1_000_000
    return ((raw + step - 1) // step) * step


def _y_ticks_for_log(weeks: list[Week]) -> tuple[float, float, list[int]] | None:
    """Log: (y_min, y_max, tick_values) или None если нет positive-значений.

    Снэпим к степеням 10. Сверху даём минимум 1 decade headroom, чтобы
    самый большой бар не упирался в потолок шкалы.

    Защита: если 0-day попадёт (data с 0-токенами, но не None) — он
    отрендерится как 2px floor bar в render-функции, см. _bar_geometry.
    """
    values = [v for w in weeks for v in w.days if v is not None and v > 0]
    if not values:
        return None
    raw_min = min(values)
    raw_max = max(values)
    exp_min = int(math.floor(math.log10(raw_min)))
    exp_max_raw = int(math.floor(math.log10(raw_max)))
    # +1 decade headroom, чтобы max-бар не лип к потолку
    exp_max = exp_max_raw + 1
    if exp_max <= exp_min:
        exp_max = exp_min + 1
    y_min = 10 ** exp_min
    y_max = 10 ** exp_max
    ticks = [10 ** e for e in range(exp_min, exp_max + 1)]
    return (float(y_min), float(y_max), ticks)


def _render_sparkline(points: list[int], color: str) -> str:
    """SVG polyline для KPI-карточки. viewBox 0 0 100 40.

    X — равномерно 0..100, Y — нормализованное значение (0=низ, 40=верх SVG).
    Edge cases:
      - 0 точек: горизонтальная линия по центру
      - 1 точка: одна точка в центре
      - все значения равны: горизонтальная линия по середине (span=1 защита от /0)
    """
    if not points:
        return (
            f'<svg viewBox="0 0 100 40" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="0,20 100,20" />'
            f"</svg>"
        )
    if len(points) == 1:
        return (
            f'<svg viewBox="0 0 100 40" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="50,20" />'
            f"</svg>"
        )
    lo, hi = min(points), max(points)
    span = max(1, hi - lo)
    coords: list[str] = []
    for i, v in enumerate(points):
        x = (i / (len(points) - 1)) * 100
        y = 40 - ((v - lo) / span) * 40
        coords.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg viewBox="0 0 100 40" preserveAspectRatio="none">'
        f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{" ".join(coords)}" />'
        f"</svg>"
    )


def _axis_labels_linear(y_max: int) -> str:
    """4 тика сверху вниз: y_max, 2/3, 1/3, 0 — компактный M-формат (2 знака)."""
    labels = [y_max, y_max * 2 // 3, y_max // 3, 0]
    return "".join(f"<span>{fmt_tokens(v)}</span>" for v in labels)


def _axis_labels_log(log_info) -> str:
    """4 тика сверху вниз: ticks[-1]..ticks[0]. Без trailing zeros."""
    if not log_info:
        return ""
    _, _, ticks = log_info
    return "".join(f"<span>{fmt_log_tick(int(t))}</span>" for t in reversed(ticks))


def _render_weekly_grid(
    weeks: list[Week], scale: str, y_info, weekly_threshold: int | None = None,
    *, now_msk: datetime,
) -> str:
    """HTML-грид 4 недели × 7 дней. Недели — колонки, дни — бары.

    Классы баров:
      .bar.history — прошедшие/текущие данные (полупрозрачный белый)
      .bar.accent  — текущий день текущей недели (фиолетовый градиент)
      .bar.future  — будущие дни или None-данные (пунктир, opacity 0.7)

    Доп. элементы:
      .bar-cell      — обёртка вокруг бара (даёт position:relative для threshold)
      .threshold     — горизонтальная пунктирная линия «потолка» сегодняшнего дня
      .threshold-label — подпись справа от линии (например «порог 10.37M»)

    Threshold рисуется ТОЛЬКО над текущим днём текущей недели (W-N, сегодня).
    Если `weekly_threshold is None` (cap превышен или days_left=0) — линия
    не рисуется. Если today_spent > threshold, бар визуально выше линии —
    это и есть сигнал «превысил, на завтра уровень пересчитается».

    `now_msk` — MSK-якорь из snapshot (ADR §2.4 / §6.4). Раньше здесь стоял
    `date.today()` (локальная TZ машины) — это был TD-2: на хостах с TZ ≠ MSK
    «сегодня» в гриде раскалывалось с реальным MSK-днём. Теперь «сегодня»
    едет из snapshot вместе со всем остальным временным контекстом билда.

    Высота — % от 260px контейнера (через _bar_height_pct).
    В шапке карточки — W-лэйбл слева + суммарный объём за неделю в M справа.
    """
    today_d = now_msk.date()
    out: list[str] = []
    for week in weeks:
        bars: list[str] = []
        for d_idx in range(7):
            value = week.days[d_idx]
            day_d = week.monday + timedelta(days=d_idx)
            if day_d > today_d:
                cls = "bar future"
                height_pct = 0.0
                title_extra = "будущее"
            elif value is None:
                cls = "bar future"
                height_pct = 0.0
                title_extra = "нет данных"
            else:
                if week.is_current and day_d == today_d:
                    cls = "bar accent"
                else:
                    cls = "bar history"
                height_pct = _bar_height_pct(value, scale, y_info)
                title_extra = fmt_int(value)
            title = f"{week.label}, {WEEKDAY_LABELS[d_idx]}: {title_extra}"
            cell_inner = (
                f'<div class="{cls}" style="height:{height_pct:.1f}%" title="{title}"></div>'
            )

            # Threshold рисуем ТОЛЬКО для текущего дня текущей недели, и только
            # если он вычислился. Линия прибита к правому краю бара (через
            # inset:0 в CSS) и выровнена по высоте threshold-значения.
            if (
                weekly_threshold is not None
                and week.is_current
                and day_d == today_d
            ):
                thr_pct = _bar_height_pct(weekly_threshold, scale, y_info)
                thr_label = f"{fmt_tokens(weekly_threshold)}"
                cell_inner += (
                    f'<div class="threshold" style="bottom:{thr_pct:.1f}%" '
                    f'title="Потолок сегодня: {fmt_int(weekly_threshold)} токенов '
                    f'(weekly cap {fmt_int(WEEKLY_CAP_TOKENS)})">'
                    f'<span class="threshold-label">{thr_label}</span>'
                    f'</div>'
                )

            bars.append(f'<div class="bar-cell">{cell_inner}</div>')
        # Сумма за неделю — только по дням с данными (None — no data, не 0).
        week_total = sum(v for v in week.days if v is not None)
        week_total_str = f"{week_total / 1_000_000:.2f}M"
        week_cls = "week current" if week.is_current else "week"
        days_html = "".join(f"<span>{lbl}</span>" for lbl in WEEKDAY_LABELS)
        out.append(
            f'<div class="{week_cls}">'
            f'<div class="week-head">'
            f'<span class="week-label">{week.label}</span>'
            f'<span class="week-total" title="Сумма за {week.label}">{week_total_str}</span>'
            f'</div>'
            f'<div class="bars">{"".join(bars)}</div>'
            f'<div class="days">{days_html}</div>'
            f"</div>"
        )
    return "".join(out)


def _render_24h_stream(bars: list[HourlyBar], today_total: int) -> str:
    """HTML-разметка 24-часового стрима.

    Структура:
      <div class="chart-shell chart-shell--24h">
        <div class="hours-24h">  — 24 ячейки .hour-cell с .bar-24h и .hour-label
      </div>

    Семантика классов бара:
      .bar-24h.active            — прошлый час с данными
      .bar-24h.peak              — топ-1 час (state="peak")
      .bar-24h.current           — текущий час (in-progress, тонкий outline)
      .bar-24h.future            — h > now.hour, пунктир
      .bar-24h.empty             — h < now.hour, value=0, нейтральный 2px floor

    height: для active/current/peak — % от max в этих часах (тот же подход, что
    в _bar_height_pct, но без log-варианта — здесь фиксированная линейная шкала:
    max всех past-часов = 100%). Для empty/future — 0% (рендерится min-height).
    """
    # Max среди прошлых/текущих часов (= потолок шкалы). Если все нули — 1,
    # чтобы не делить на 0; пустые бары всё равно рендерятся как min-height.
    past_values = [b.value for b in bars if b.state in ("active", "current", "peak")]
    scale_max = max(past_values) if past_values else 1

    def _pct(v: int) -> float:
        if v <= 0 or scale_max <= 0:
            return 0.0
        return max(2.0, min(100.0, (v / scale_max) * 100))

    cells: list[str] = []
    for b in bars:
        # Класс бара: один из active/peak/current/future/empty — все три
        # "с данными" (active/peak/current) рендерятся одним цветом
        # (см. CSS), intensity-* классы больше не добавляются.
        if b.state == "peak":
            cls = "bar-24h peak"
            height_pct = _pct(b.value)
        elif b.state == "current":
            cls = "bar-24h current"
            height_pct = _pct(b.value)
        elif b.state == "active":
            cls = "bar-24h active"
            height_pct = _pct(b.value)
        elif b.state == "future":
            cls = "bar-24h future"
            height_pct = 0.0
        else:  # "empty"
            cls = "bar-24h empty"
            height_pct = 0.0

        title = f"{b.hour:02d}:00–{b.hour:02d}:59: {fmt_int(b.value) if b.value else 'нет данных'}"
        label_cls = "hour-label" + (" hour-label--future" if b.state == "future" else "")
        # Лейбл значения над peak-баром (TL, 2026-08-05): для остальных
        # ячеек не рендерим — bar layout не сдвигается (absolute positioning
        # относительно .hour-cell, top:5px).
        peak_value_html = (
            f'<span class="peak-value">{fmt_tokens(b.value)}</span>'
            if b.state == "peak" else ""
        )
        cells.append(
            f'<div class="hour-cell" data-hour="{b.hour}">'
            f'{peak_value_html}'
            f'<div class="{cls}" style="height:{height_pct:.1f}%" title="{title}"></div>'
            f'<span class="{label_cls}">{b.hour:02d}</span>'
            f"</div>"
        )

    # Без NOW-маркера (по TL review, 2026-08-04): визуальный шум на широкой
    # карточке 24-баров, «сейчас» и так понятно из meta-строки карточки
    # (`Всего · Пик: HH:00 (N.NNM)` — running total до текущего часа включительно
    # однозначно фиксирует позицию «now» в дня). Текущий час визуально
    # отличается тонким outline (.bar-24h.current) — этого достаточно.

    return (
        f'<div class="chart-shell chart-shell--24h">'
        f'<div class="hours-24h">{"".join(cells)}</div>'
        f"</div>"
    )


def _build_hero_pill_inner(
    *,
    actual: int | None,
    actual_paren: int | float | None,
    cap: int | None,
    cap_paren: int | float | None,
    level: str,
    sep: str = "/",
    paren_inline: bool = False,
) -> str:
    """Inner markup (дети) `.hero-pill` для трио actual · sep · cap.

    Используется в двух местах:
      - `_render_hero_pill` оборачивает результат в `<span class="hero-pill">`
        (standalone day-pill).
      - `_render_combined_session_pill` кладёт результат в `<span
        class="hero-pill__ratio">` внутри большого combined pill'а.

    Параметры:
      - sep — разделитель между actual и cap. По умолчанию '/', для
        combined-pill'а вызывающий код передаёт '•' (bullet — это ratio,
        не деление).
      - paren_inline — True → скобочный суффикс вложен ВНУТРЬ value-span'а,
        без flex-gap между числом и скобками (плотный вид `273.8K(5)`).
        False (default, standalone pill) → суффикс отдельным flex-child'ом,
        6px gap от родительского `.hero-pill` даёт воздух `273.8K (5)`.

    None в actual → fmt_tokens рендерит "—"; в cap — "—" + level="none"
    (без цветового класса). paren=None → блок скобок не рендерится вообще.
    """
    # level: пробрасывается вызывающим кодом (ADR §2.2 rule 8).
    # analytics.build_snapshot вычисляет weekly.day_level / session.level
    # через чистую функцию из analytics; render-сторона использует готовый
    # level и НЕ импортирует compute-функции (направление импорта
    # render -> analytics однонаправленное, см. ADR §2.2 правило 8).
    actual_str = fmt_tokens(actual)
    cap_str = fmt_tokens(cap) if cap is not None else "—"

    actual_cls = "hero-pill__actual"
    if level != "none":
        actual_cls += f" hero-pill__actual--{level}"

    def _value_group(value_str: str, value_cls: str, paren: int | float | None) -> str:
        paren_html = ""
        if paren is not None:
            content = fmt_avg(paren) if isinstance(paren, float) else paren
            paren_html = f'<span class="hero-pill__paren">({content})</span>'
        if paren_inline and paren_html:
            # Скобка ВНУТРИ value-span'а → flex-gap родителя не разделяет
            # число и скобки, видим как `273.8K(5)`.
            return f'<span class="{value_cls}">{value_str}{paren_html}</span>'
        # Скобка отдельным flex-child'ом → 6px gap от `.hero-pill`,
        # видим как `273.8K (5)`.
        return f'<span class="{value_cls}">{value_str}</span>{paren_html}'

    return (
        _value_group(actual_str, actual_cls, actual_paren)
        + f'<span class="hero-pill__sep">{sep}</span>'
        + _value_group(cap_str, "hero-pill__cap", cap_paren)
    )


def _render_hero_pill(
    actual: int | None,
    actual_paren: int | float | None,
    cap: int | None,
    cap_paren: int | float | None,
    title: str,
    level: str,
    sep: str = "/",
    paren_inline: bool = False,
) -> str:
    """Один pill формата `actual(act_paren) <sep> cap(cap_paren)`.

    Standalone-обёртка над `_build_hero_pill_inner` — добавляет внешний
    `<span class="hero-pill" title="...">`. Используется ТОЛЬКО для day-pill'а
    (today_tokens / weekly_threshold). Session-pill переехал в combined
    (`_render_combined_session_pill`), который вызывает inner напрямую.

    Числитель подсвечивается через .hero-pill__actual--{ok|warn|over|none}
    по результату pill_level(actual, cap). Знаменатель — нейтральный белый.
    Скобочные суффиксы — вторичная метрика, отдельный класс, без подсветки.

    None в actual / cap → рендерим "—" (как fmt_tokens для None), класс уровня
    не добавляем (нейтральный цвет). paren=None → без скобок вообще.
    """
    inner = _build_hero_pill_inner(
        actual=actual,
        actual_paren=actual_paren,
        cap=cap,
        cap_paren=cap_paren,
        level=level,
        sep=sep,
        paren_inline=paren_inline,
    )
    return f'<span class="hero-pill" title="{title}">{inner}</span>'


def _render_combined_session_pill(
    *,
    title: str | None,
    session_record_title: str | None = None,
    duration_ms: int | None,
    path: str | None,
    actual: int | None,
    actual_paren: int | float | None,
    cap: int | None,
    cap_paren: int | float | None,
    level: str,
) -> str:
    """Combined pill «session_title • project | duration | actual(act_p) • cap(cap_p)» слева в hero-полосе.

    Структура:
      <span class="hero-pill hero-pill--combined" title="<path>\\n\\n<ratio-desc>">
        <span class="hero-pill__session">{session_record_title}</span>   <!-- опционально -->
        <span class="hero-pill__sep-dot">•</span>                         <!-- опционально -->
        <span class="hero-pill__project">{project_title}</span>
        <span class="hero-pill__duration">{duration}</span>   <!-- border-left = `|` -->
        <span class="hero-pill__ratio">                       <!-- border-left = `|` -->
          <span class="hero-pill__actual …">…<span class="hero-pill__paren">(…)</span></span>
          <span class="hero-pill__sep">•</span>
          <span class="hero-pill__cap">…<span class="hero-pill__paren">(…)</span></span>
        </span>
      </span>

    Семантика:
      - session_record_title — имя ветки/работы (record_json.title,
        "_fetch_session_title"), опционально. None/пусто → блок + разделитель
        НЕ рендерятся, остаётся только project (старый fallback 2026-08-04).
      - title (project) — короткое имя проекта (последняя папка пути без
        NNNN_-префикса), либо '—' если путь не определён. html.escape обязателен
        (приходит из workspaceDir, может содержать &, <, >, кавычки).
      - duration — fmt_duration(duration_ms), '—' если None.
      - path уходит в HTML title (tooltip) — на самом pill'е не показываем,
        чтобы не раздувать. Если path=None, в tooltip кладём "путь не определён".
        Дополнительно вторым абзацем в tooltip — пояснение к ratio (сохранено
        из старого standalone session-pill'а).
      - ratio — вызов `_build_hero_pill_inner` с sep='•' и paren_inline=True.
        • вместо / потому что actual/cap — это ratio (текущая vs средняя), а
        не деление. paren_inline=True → скобки вложены внутрь value-span'ов,
        видим как `273.8K(5) • 877.5K(5.7)` (плотный вид, без 6px-flex-gap
        между числом и скобками).

    TL-фидбэк 2026-08-05: до этого context и session были двумя отдельными
    pill'ами в hero-полосе и не влезали в medium-viewport. Объединили в один
    pill — визуально «какая сессия + как давно открыта + расход» читается
    одним блоком. Затем 2026-08-05 добавлен session_record_title (имя ветки) —
    он семантически отличается от project (папка) и заслуживает отдельной
    позиции. На типичной ширине viewport'а умещается; если нет — ellipsis
    на обоих span'ах не даёт сломать layout.
    """
    title_str = html.escape(title or "—")
    duration_str = fmt_duration(duration_ms)
    ratio_desc = "Токены в текущей сессии (запросы) / среднее по сессиям (запросы/сессию)"
    # Собираем tooltip одной строкой и escape'им один раз. quote=True — потому
    # что tooltip кладётся в HTML-атрибут title="…", а кавычки в path (если
    # есть) ломают атрибут. escape() по умолчанию не эскейпит ", поэтому
    # включаем явно. Двойной escape (path + tooltip) дал бы &amp;quot; вместо
    # &quot; — собираем сырые значения и эскейпим один раз.
    tooltip_raw = f"{path or 'путь не определён'}\n\n{ratio_desc}"
    tooltip_attr = html.escape(tooltip_raw, quote=True)
    ratio_inner = _build_hero_pill_inner(
        actual=actual,
        actual_paren=actual_paren,
        cap=cap,
        cap_paren=cap_paren,
        level=level,
        sep="•",
        paren_inline=True,
    )
    # session_title — опциональная префиксная часть. Рендерим отдельным span'ом
    # с собственным классом (стиль отличается от project, см. CSS), плюс
    # отдельный sep-dot span между session и project (т.к. .hero-pill__sep
    # внутри .hero-pill__ratio — это ratio-разделитель, у него свой контекст
    # и цвет; во избежание коллизий и для независимой стилизации — новый класс).
    if session_record_title:
        session_title_str = html.escape(session_record_title)
        session_block = (
            f'<span class="hero-pill__session">{session_title_str}</span>'
            f'<span class="hero-pill__sep-dot">•</span>'
        )
    else:
        session_block = ""
    return (
        f'<span class="hero-pill hero-pill--combined" title="{tooltip_attr}">'
        f"{session_block}"
        f'<span class="hero-pill__project">{title_str}</span>'
        f'<span class="hero-pill__duration">{duration_str}</span>'
        f'<span class="hero-pill__ratio">{ratio_inner}</span>'
        f"</span>"
    )


def _render_hero_pills(
    *,
    current_session_tokens: int | None,
    current_session_requests: int,
    current_session_title: str | None,
    current_session_record_title: str | None,
    current_session_duration_ms: int | None,
    current_session_path: str | None,
    avg_tokens_per_session: int | None,
    today_avg: float,
    today_tokens: int,
    weekly_threshold: int | None,
    day_level: str,
    session_level: str,
) -> str:
    """Два pill'а в hero-полосе: combined(session+context) · day.

    - combined (слева): «<session_title> • <project> | <duration> | <actual(act_p)> • <cap(cap_p)>».
      Один pill вместо двух — context и session объединены 2026-08-05, чтобы
      освободить горизонтальное место в hero-полосе (два pill'а не влезали на
      medium-viewport). Bullet `•` вместо `/` — ratio, не деление.
      session_title (record_json.title) и project (workspaceDir) — РАЗНЫЕ сущности
      (имя ветки vs имя папки), 2026-08-05 добавлены оба через разделитель •.
      Если session_title=None — рендерится только project (старый fallback).
    - day (справа): today_tokens / weekly_threshold.
      Знаменатель — рассчитанный потолок на сегодня (см. compute_weekly_threshold).

    Pill с пустым знаменателем (нет данных за день / threshold=None / current_session
    нет) рендерится как "—" со neutral-цветом, чтобы layout не «скакал» между билдами.
    """
    combined_pill = _render_combined_session_pill(
        title=current_session_title,
        session_record_title=current_session_record_title,
        duration_ms=current_session_duration_ms,
        path=current_session_path,
        actual=current_session_tokens,
        actual_paren=current_session_requests,
        cap=avg_tokens_per_session,
        cap_paren=today_avg,
        level=session_level,
    )
    day_pill = _render_hero_pill(
        actual=today_tokens,
        actual_paren=None,
        cap=weekly_threshold,
        cap_paren=None,
        title="Потрачено сегодня / рассчитанный потолок дня (weekly cap / days_left)",
        level=day_level,
    )
    return f'<div class="hero-pills">{combined_pill}{day_pill}</div>'





def render(snapshot: dict) -> str:
    # ---- snapshot unpack (ADR §2.4) ----
    now_msk: datetime = snapshot["now_msk"]
    hour: dict = snapshot["hour"]
    today: dict = snapshot["today"]
    window: dict = snapshot["window"]
    weekly: dict = snapshot["weekly"]
    session: dict = snapshot["session"]

    weeks: list[Week] = weekly["weeks"]
    weekly_threshold: int | None = weekly["threshold"]
    today_24h: list[HourlyBar] = today["bars_24h"]
    today_24h_peak: tuple[int, int] | None = today["peak_24h"]

    # Алиасы под старые kwarg-имена render_html — f-string template
    # (ниже) использует их без переписывания. Семантика не меняется:
    # это одни и те же поля, просто короткие имена для удобства markup'а.
    current_hour_tokens: int = hour["tokens"]
    today_tokens: int = today["tokens"]
    today_sessions: int = today["sessions"]
    today_user_requests: int = today["user_requests"]
    today_avg: float = today["avg"]
    avg_tokens_per_session: int | None = today["avg_tokens_per_session"]
    window_total: int = window["total"]

    # derived (UI-only, не из snapshot — это геометрия представления)
    y_max: int = _y_max_for(weeks)
    log_info: tuple[float, float, list[int]] | None = _y_ticks_for_log(weeks)

    # ---- KPI time-labels (из now_msk) ----
    today_label = now_msk.strftime("%Y-%m-%d %H:%M MSK")
    today_date = today_label[:10]
    cur_h = now_msk.hour
    cur_time = f"{cur_h:02d}:00–{cur_h:02d}:59, {today_date}"
    today_time = f"00:00–{cur_h:02d}:59"
    if window["wraps"]:
        window_time = f"{window['label']}, вчера→сегодня"
    else:
        window_time = f"{window['label']}, сегодня"

    # ---- weekly grid (linear + log) ----
    linear_grid = _render_weekly_grid(weeks, "linear", y_max, weekly_threshold, now_msk=now_msk)
    log_grid = (
        _render_weekly_grid(weeks, "log", log_info, weekly_threshold, now_msk=now_msk) if log_info else ""
    )
    axis_linear = _axis_labels_linear(y_max)
    axis_log = _axis_labels_log(log_info)

    if log_info:
        log_meta_range = (
            f"{fmt_tokens(int(log_info[0]))} … {fmt_tokens(int(log_info[1]))}"
        )
    else:
        log_meta_range = "—"

    # ---- 24h stream meta ----
    if today_24h_peak is not None:
        peak_h, peak_v = today_24h_peak
        peak_meta = (
            f"Пик: <strong>{peak_h:02d}:00</strong> "
            f"({fmt_tokens(peak_v)})"
        )
    else:
        peak_meta = "Пик: <strong>—</strong>"
    stream_total = sum(b.value for b in today_24h if b.value > 0)

    # ---- hero pills (level пробрасывается из snapshot) ----
    hero_pills = _render_hero_pills(
        current_session_tokens=session["tokens"],
        current_session_requests=session["requests"],
        current_session_title=session["project_title"],
        current_session_record_title=session["record_title"],
        current_session_duration_ms=session["duration_ms"],
        current_session_path=session["path"],
        avg_tokens_per_session=today["avg_tokens_per_session"],
        today_avg=today["avg"],
        today_tokens=today["tokens"],
        weekly_threshold=weekly_threshold,
        day_level=weekly["day_level"],
        session_level=session["level"],
    )

    # === ниже — старая логика render_html до return (читает локалы выше).
    # Оставляем её без изменений: derived переменные (cur_time, today_time,
    # linear_grid, log_grid, axis_linear, axis_log, log_meta_range,
    # peak_meta, stream_total, hero_pills, WEEKLY_CAP_TOKENS, fmt_*) уже
    # доступны. ===

    # Шапка weekly-карточки использует текущий недельный лейбл; в старом коде
    # переменная current_week_label, но в markup она не подставляется
    # (использовалась только в логе; не критично, оставлено на случай если
    # кто-то добавит). Не удаляем чтобы не ломать byte-identity.

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="refresh" content="60" />
  <title>Token Dashboard — {today_label}</title>
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
      --history: rgba(255, 255, 255, 0.18);
      --history-2: rgba(255, 255, 255, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Inter", "Segoe UI Variable", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(139, 92, 246, 0.16), transparent 26%),
        linear-gradient(180deg, #0e1014 0%, #11141a 100%);
    }}
    .shell {{ width: min(1280px, calc(100vw - 36px)); margin: 24px auto 36px; }}
    .topbar {{
      display: flex; justify-content: space-between; align-items: center; gap: 16px;
      margin-bottom: 16px; color: var(--muted);
      font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.16em;
    }}
    .topbar a {{ color: inherit; text-decoration: none; }}
    .hero {{ margin-bottom: 18px; }}
    .panel {{ border: 1px solid var(--line); border-radius: 24px; background: var(--panel); }}
    .hero-card {{
      padding: 26px 28px 22px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0)),
        radial-gradient(circle at 100% 0%, rgba(139,92,246,0.13), transparent 30%),
        var(--panel);
    }}
    .hero-top {{
      display: flex; justify-content: space-between; align-items: center; gap: 20px;
      padding-bottom: 18px; border-bottom: 1px solid rgba(255,255,255,0.06);
    }}
    /* h1: удалён вместе с <h1>Расход токенов runtime</h1> в hero — его роль
       взял <p class="eyebrow">Token Usage</p>. Если когда-то понадобится
       большой заголовок страницы, вернуть вместе с <h1> в markup. */
    .tz-chip {{
      padding: 8px 14px; border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;
      background: rgba(255,255,255,0.03); color: #8ea3c7;
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace; font-size: 13px; line-height: 1.35;
    }}
    /* ===== Hero Pills (header strip) =====
       Промежуточная зона между hero-top и tz-chip (раньше была пустой —
       красная рамка в макете 2026-08-05). Два pill'а:
         - "combined" (слева): project | duration | actual(req) • cap(avg_req).
           Объединяет бывшие context+session pill'ы (TL-фидбэк 2026-08-05) — два
           pill'а не влезали в hero-полосу на medium-viewport. Bullet `•` вместо
           `/` потому что actual/cap — это ratio (текущая vs средняя), а не
           деление.
         - "day" (справа): today_tokens / weekly_threshold.
       Числитель (.hero-pill__actual) подсвечивается по pill_level (зелёный /
       оранжевый / красный); знаменатель (.hero-pill__cap) — всегда нейтральный
       белый, чтобы цвет числителя не «зашумлялся». Стиль крупнее, чем
       .kpi-pill: больше padding, размер шрифта, акцентные border-radius. */
    .hero-pills {{
      display: flex; flex: 1 1 auto; justify-content: flex-end;
      align-items: end; gap: 10px; flex-wrap: wrap;
    }}
    .hero-pill {{
      display: inline-flex; align-items: baseline; gap: 6px;
      padding: 8px 14px; border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.03);
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 13px; line-height: 1.35;
      white-space: nowrap;
    }}
    .hero-pill__actual {{
      font-weight: 700; color: var(--ink);
    }}
    .hero-pill__actual--ok   {{ color: #10b981; }}  /* зелёный, ≤80% */
    .hero-pill__actual--warn {{ color: #f59e0b; }}  /* оранжевый, 80-100% */
    .hero-pill__actual--over {{ color: #ef4444; }}  /* красный, >100% */
    .hero-pill__sep {{ color: var(--muted); font-weight: 400; }}
    .hero-pill__cap {{ color: #e6ebf6; font-weight: 600; }}
    .hero-pill__paren {{ color: #8ea3c7; font-weight: 500; font-size: 12px; }}
    /* Combined pill (project | duration | actual(act_p) • cap(cap_p)).
       Семантически: «что за проект + как давно открыта сессия + текущая сессия
       vs средняя». Три группы через `|` (border-left на следующих группах):
         - project  (var(--ink), bold 800, ellipsis >260px)
         - duration (серый, font-size 12px, border-left = `|`)
         - ratio    (.hero-pill__ratio wrapper, border-left = `|`, gap 6px внутри)
       gap:8px между группами — чуть больше, чем у baseline-pill'а (6px), чтобы
       визуально отделить «имя» от «метрики». align-items:center — duration и
       ratio могут быть ниже project (font-size 12px) по baseline; центрирование
       по высоте pill'а выравнивает их посередине.
       - .hero-pill__project — bold, var(--ink), ellipsis при переполнении.
         max-width:260px — рассчитано на 30-40 символов, на типичных именах
         папок ('college-publisher', 'agent-tokens-dashboard') влезает целиком,
         на аномально длинных — обрезается с многоточием.
       - .hero-pill__duration — серый, шрифт на 1px меньше, отделён вертикальной
         палочкой слева. Контраст с ярким .hero-pill__project даёт визуальный
         «псевдо-ratio» без введения новой семантики.
       - .hero-pill__ratio — внутренняя мини-полоса pill'а (actual • cap).
         display:inline-flex + gap:6px — gap внутри ratio отличается от gap
         между группами (8px у .hero-pill--combined), чтобы ratio читался
         «плотнее». border-left + padding-left — даёт второй `|` после duration. */
    .hero-pill--combined {{ gap: 8px; align-items: center; }}
    /* .hero-pill__session — имя ветки/работы (record_json.title), 2026-08-05.
       Семантически отличается от project (папка репозитория): первое — что
       делаем, второе — где. Стиль — второстепенный (muted, шрифт 12px), чтобы
       project оставался "главным именем" слева, а session — пояснением.
       .hero-pill__sep-dot — разделитель • между session и project, отдельный
       класс чтобы НЕ путать с .hero-pill__sep (это ratio-разделитель внутри
       .hero-pill__ratio, у него своё оформление и контекст).
       max-width: 220px — рассчитано на типичные имена веток ("TB07 Idempotency
       Photos", "Fix dashboard peak label"); на аномально длинных — ellipsis,
       layout не ломается. */
    .hero-pill__session {{
      color: #c7d0e2; font-weight: 600; font-size: 12px;
      max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    .hero-pill__sep-dot {{ color: var(--muted); font-weight: 400; }}
    .hero-pill__project {{
      font-weight: 800; color: var(--ink);
      max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    .hero-pill__duration {{
      color: #8ea3c7; font-weight: 600; font-size: 12px;
      border-left: 1px solid rgba(255,255,255,0.14); padding-left: 8px;
    }}
    .hero-pill__ratio {{
      display: inline-flex; align-items: baseline; gap: 6px;
      border-left: 1px solid rgba(255,255,255,0.14); padding-left: 8px;
    }}
    .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-top: 18px; }}
    .kpi {{
      position: relative;            /* якорь для ::before-полоски */
      padding: 18px 18px 16px; min-height: 168px;
      display: flex; flex-direction: column;
      border-radius: 10px 22px 22px 10px;   /* override .panel: слева меньше,
                                              чтобы полоска читалась как "загиб" */
      background: linear-gradient(180deg, rgba(255,255,255,0.015), rgba(255,255,255,0)), var(--panel-2);
      overflow: hidden;              /* ::before-полоска наследует border-radius .kpi */
    }}
    /* Акцентная вертикальная полоска слева, как в референсе 2026-08-05
       (6 нижних карточек-метрик). Тонкая (3px), на всю высоту карточки,
       повторяет скругление .kpi слева. Цвет управляется --kpi-accent
       через модификаторы .kpi--yellow/--blue/--green/--orange. */
    .kpi::before {{
      content: "";
      position: absolute;
      left: 0; top: 0; bottom: 0;
      width: 3px;
      background: var(--kpi-accent, #facc15);
      border-radius: 10px 0 0 10px;
      pointer-events: none;
    }}
    .kpi--yellow {{ --kpi-accent: #facc15; }}  /* ТЕКУЩИЙ МОМЕНТ */
    .kpi--blue   {{ --kpi-accent: #3b82f6; }}  /* СЕГОДНЯ */
    .kpi--green  {{ --kpi-accent: #10b981; }}  /* РАБОЧЕЕ ВРЕМЯ */
    .kpi--orange {{ --kpi-accent: #fb923c; }}  /* АКТИВНОСТЬ */
    .kpi-head {{ display: flex; justify-content: space-between; align-items: start; gap: 10px; }}
    .kpi-title {{
      color: rgba(216, 223, 236, 0.54);
      font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.16em;
    }}
    .kpi-time {{ margin-top: 6px; color: #96a0b5; font-size: 13px; line-height: 1.35; }}
    /* .kpi-pill — маленькая "таблетка" в правом верхнем углу карточки KPI.
       Сейчас используется только в карточке «Активность» для avg tokens / session
       (≈ N/сессию), но стиль общий — пригодится и для других мета-метрик.
       align-self:start — на случай, если родитель — flex column, держим top.
       white-space:nowrap — внутри pill сидят number + "/сессию", перенос ломает
       компактный вид (две строки в крошечном боксе). */
    .kpi-pill {{
      align-self: start;
      padding: 4px 9px;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 999px;
      background: rgba(255,255,255,0.03);
      color: #96a0b5;
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 11px; line-height: 1.4;
      white-space: nowrap;
    }}
    .kpi-pill strong {{ color: #e6ebf6; font-weight: 700; }}
    /* .kpi-value: прижат к низу карточки через margin-top:auto (родитель .kpi
       — flex column). Спарклайны удалены из карточек 1-3, поэтому раньше
       .spark давал визуальный низ; теперь его роль играет .kpi-value.
       Карточка 4 использует .kpi-fraction с тем же приёмом. */
    .kpi-value {{
      margin-top: auto;
      padding-top: 24px;
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 68px; line-height: 1; letter-spacing: -0.05em; font-weight: 800;
    }}
    /* Дробь в карточке «Активность» (4-я в ряду): big req/sess слева,
       справа — вертикальный стек user_requests / sessions с разделителем.
       Семантика: 4 = 12 user requests / 3 sessions. */
    .kpi-fraction {{
      display: flex; align-items: center; gap: 14px;
      margin-top: auto;
      padding-top: 24px;
    }}
    .kpi-fraction__value {{
      margin-top: 0;  /* отменяем .kpi-value margin-top:auto внутри флекс-контейнера */
    }}
    .kpi-fraction__stack {{
      display: flex; flex-direction: column; align-items: stretch;
      min-width: 56px;
    }}
    .kpi-fraction__num,
    .kpi-fraction__den {{
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 22px; line-height: 1; font-weight: 700;
      color: #e6ebf6;
      padding: 2px 0;
    }}
    .kpi-fraction__divider {{
      height: 1px; background: rgba(255,255,255,0.18); margin: 6px 0;
    }}
    .chart-panel {{ padding: 24px; }}
    /* Нижний отступ chart-секций такой же, как у .hero (18px) — чтобы
       gap между weekly и 24H совпадал с gap между hero и weekly.
       :not(:last-child) чтобы не дублировать shell margin-bottom у
       последней секции (там и так 36px от .shell). */
    .chart-panel:not(:last-child) {{ margin-bottom: 18px; }}
    /* .eyebrow — единственный «заголовок» карточки. margin-bottom:0 потому,
       что под ним больше нет h2.chart-title (он удалён вместе с .chart-title):
       нижний отступ до легенды уже задан в .chart-head {{ margin-bottom: 18px }}. */
    .eyebrow {{
      margin: 0; color: var(--muted);
      font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.18em;
    }}
    /* .eyebrow__date — параметр-инлайн внутри eyebrow (например, дата дня для
       24h-карточки). Толщина +1 ступень от eyebrow (700→800), цвет +1 ступень
       ярче var(--muted) — используем #e6ebf6, тот же акцент-цвет, что и
       .kpi-pill strong. letter-spacing:0 — чтобы цифры даты не «расплывались»
       на 0.18em, унаследованной от .eyebrow. */
    .eyebrow__date {{
      margin-left: 14px;
      font-weight: 800;
      color: #e6ebf6;
      letter-spacing: 0;
    }}
    .chart-head {{
      display: flex; justify-content: space-between; align-items: center; gap: 16px;
      margin-bottom: 18px;
    }}
    .chart-meta {{
      display: flex; align-items: center; gap: 18px; color: var(--muted); font-size: 14px;
      flex-wrap: wrap; justify-content: flex-end;
    }}
    .scale-toggle {{
      display: inline-flex; padding: 4px; border-radius: 999px;
      background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    }}
    .scale-toggle__btn {{
      border: 0; background: transparent; padding: 6px 12px; border-radius: 999px;
      color: #9ca3af; font-size: 12px; font-weight: 700; text-transform: uppercase;
      cursor: pointer; transition: background 0.15s ease, color 0.15s ease;
    }}
    .scale-toggle__btn.is-active {{ background: rgba(255,255,255,0.94); color: #111827; }}
    .scale-toggle__btn:hover:not(.is-active) {{ color: var(--ink); }}
    /* Видимость вариантов чарта и meta-текста по data-scale. */
    .chart-panel[data-scale="linear"] .chart-variant--log {{ display: none; }}
    .chart-panel[data-scale="log"]    .chart-variant--linear {{ display: none; }}
    .chart-panel[data-scale="linear"] .chart-meta__log {{ display: none; }}
    .chart-panel[data-scale="log"]    .chart-meta__linear {{ display: none; }}
    .legend {{
      display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 16px;
      color: var(--muted); font-size: 13px;
    }}
    .dot {{
      display: inline-block; width: 10px; height: 10px; margin-right: 6px;
      border-radius: 999px; vertical-align: middle;
    }}
    /* .week-cap-pill — компактная "таблетка" в одной линии со шкалой и тогглом
       в .chart-meta. Заменил пояснительную легенду (TL review, 2026-08-05):
       "W прошлые/current" читается из самих баров, threshold-инфо сворачивается
       в одну пилюлю "Порог недели: 60.00M". Цвет strong = var(--ink) — тот же
       белый, что у weekly-total (78.99M, 67.72M) и у threshold-линии/подписи
       на текущей неделе, чтобы пилюля, пунктир и подпись "10.61M" читались
       как одна семантическая группа. */
    .week-cap-pill {{
      display: inline-flex; align-items: center;
      padding: 4px 10px;
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 999px;
      background: rgba(255,255,255,0.03);
      color: var(--muted);
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 11px; line-height: 1.4;
      white-space: nowrap;
    }}
    .week-cap-pill strong {{
      margin-left: 6px;
      color: var(--ink);
      font-weight: 700;
    }}
    .chart-shell {{
      padding: 18px; border-radius: 22px;
      background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0)), #141820;
      border: 1px solid rgba(255,255,255,0.04);
    }}
    .plot {{ display: grid; grid-template-columns: 94px 1fr; gap: 18px; }}
    /* Шкала (log и linear): одна координатная система для баров, лейблов и гридлайнов.
       Диапазон 0%..100% .axis = 100M..100K (лог) или y_max..0 (linear).
       4 лейбла: 0% (top = 100M/y_max), 29.7% (10M / 2y_max/3),
                  59.4% (1M / y_max/3), 89.08% (100K / 0).
       .bars тоже занимает 0%..(100% - 10.92%) = ровно тот же диапазон, поэтому
       height:X% на .bar автоматически ложится на те же Y, что гридлайны и лейблы. */
    .axis {{
      position: relative; color: #8e97a8; font-size: 13px;
    }}
    .axis span {{
      position: absolute; right: 10px; line-height: 1;
    }}
    /* :nth-child по порядку рендера: 1=100M (лог) / y_max (linear), 2=10M/2y_max/3,
       3=1M/y_max/3, 4=100K/0. 4-й лейбл сидит ровно на 0M — на логе 0M ≡ 100K.
       translateY(-50%) центрирует строку подписи по тем же Y, что гридлайны. */
    .axis span:nth-child(1) {{ top: 0; }}
    .axis span:nth-child(2) {{ top: 29.7%; transform: translateY(-50%); }}
    .axis span:nth-child(3) {{ top: 59.4%; transform: translateY(-50%); }}
    .axis span:nth-child(4) {{ top: 89.08%; transform: translateY(-50%); }}
    /* .weeks — без padding-top, без красных линий. Только серые гридлайны на
       29.7% / 59.4% (на тех же Y, что центры 10M/1M или 2y_max/3, y_max/3).
       grid-stretch в .plot даёт .axis ту же высоту, что и .weeks (= .week). */
    .weeks {{
      display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
      background: linear-gradient(
        180deg,
        transparent 0 calc(29.7% - 0.5px),
        var(--grid) calc(29.7% - 0.5px) calc(29.7% + 0.5px),
        transparent calc(29.7% + 0.5px) calc(59.4% - 0.5px),
        var(--grid) calc(59.4% - 0.5px) calc(59.4% + 0.5px),
        transparent calc(59.4% + 0.5px) 100%
      );
    }}
    .week {{
      /* Chart area: 0%..89.08% от .week (это и есть шкала: 100M на top, 100K на 89.08%).
         .week-head и .days вынесены absolute по краям и лежат ПОВЕРХ .bars (z-index),
         поэтому бары реально занимают весь диапазон 0%..89.08%, а не подрезанный
         .week-head'ом сверху. min-height фиксирует высоту карточки, чтобы проценты
         гридлайнов и баров сходились (без него .week схлопывается, т.к. абсолютные
         дети не участвуют в auto-flow). */
      position: relative;
      padding: 0 12px 12px;  /* top padding уехал на top:12px у .week-head */
      aspect-ratio: 1 / 1;   /* карточка недели = квадрат: высота = ширине колонки грида.
                                .weeks — grid-template-columns: repeat(4, 1fr) → ширина
                                колонки ≈ (chart-shell-inner - 94px-axis - 18px-gap - 16px*3) / 4.
                                На 1280px-viewport это ~259px (был min-height: 360px,
                                теперь height=259px, ~28% короче). Все вертикальные
                                позиции (.bars top:0/bottom:38px, .axis на 0/29.7/59.4/89.08%,
                                threshold bottom:X%, threshold-label top:-10px) — в px или % от
                                .week/.bars, поэтому shared coord system сохраняется
                                (бар 10M топом = 10M-лейбл = 29.7% .week) и линия threshold
                                остаётся на той же Y относительно "14.97M", что и раньше. */
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
      border: 1px solid rgba(255,255,255,0.03);
    }}
    .week.current {{
      background: linear-gradient(180deg, rgba(139,92,246,0.10), rgba(139,92,246,0.03));
      border-color: rgba(139,92,246,0.22);
      box-shadow: inset 0 0 0 1px rgba(139,92,246,0.06);
    }}
    .week-head {{
      /* Абсолютно сверху карточки, поверх баров. top:12px даёт ту же воздушную
         прослойку, что padding-top:12px в старом лейауте; z-index:3 перекрывает
         самый высокий бар (100M = top of .bars). */
      position: absolute; top: 12px; left: 12px; right: 12px; z-index: 3;
      display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
      margin: 0;  /* margin-bottom:12px в старом лейауте больше не нужен — .bars сам
                      позиционируется сверху через top:0 */
      color: var(--muted);
      font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.14em;
    }}
    .week-total {{
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 12px; font-weight: 700; color: var(--ink);
      text-transform: none; letter-spacing: -0.01em;
    }}
    .week.current .week-total {{ color: var(--accent); }}
    /* .bars — absolute, занимает ровно 0%..(100% - 10.5%) .week.
       bottom:10.5% (вместо 38px) — чтобы при квадратной карточке (aspect-ratio:1,
       .week-height = .week-width ≈ 259px) shared coord system с осью не
       разъезжалась: иначе .bars занимал бы 0..85.33% карточки, ось 1M (на 59.4%
       .week) «уезжала» в .bars на 69.6% вместо 66.4%, и бар 872K (height:31.4%
       .bars) визуально оказывался ВЫШЕ линии 1M, хотя по значению должен быть
       ниже. 10.5% = 38/360 в исходном калибре (min-height:360px) — теперь
       работает при любой высоте .week.

       bottom:38px в старом лейауте калибровался на min-height:360px и означал:
       1px border-top + 10px padding-top + ~14px line-height + 12px .week
       padding-bottom + 1px буфер ≈ 38px. С .week-height = 259px (квадрат)
       абсолютный 38px становится 14.67% карточки, и 100K-анкер (top:89.08%
       .week) уезжает в .days-зону, а 1M/100K-бары — выше своих лейблов.

       bar's height:X% теперь = X% от 0%..89.5% .week на любой высоте, и
       10M-бар (X=66.7%) топом = 10M-лейбл (29.7% .week) ±округление.
       Порог (bottom:X% .bar-cell) и threshold-label (top:-10px) пересчитываются
       автоматически. */
    .bars {{
      position: absolute; top: 0; left: 12px; right: 12px; bottom: 10.5%;
      display: flex; gap: 7px; z-index: 1;
    }}
    .bar-cell {{
      /* Обёртка вокруг одного бара. position:relative — для абсолютного
         позиционирования threshold-линии; сам .bar — flex-child .bar-cell,
         а не .bars — иначе threshold не привязать к ширине одного дня.
         align-items: stretch в .bars растягивает .bar-cell на всю высоту
         .bars, без чего height:N% на .bar уехал бы в 0. */
      position: relative; flex: 1; min-width: 0; min-height: 6px;
      display: flex; align-items: end;
    }}
    .bar {{
      width: 100%; border-radius: 10px 10px 0 0; min-height: 6px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.06);
    }}
    .bar.history {{ background: var(--history); }}
    .bar.accent  {{ background: linear-gradient(180deg, #a78bfa, #8b5cf6); }}
    .bar.future {{
      background: rgba(255,255,255,0.03);
      border: 1px dashed rgba(255,255,255,0.12);
      opacity: 0.7; box-shadow: none;
    }}
    /* Threshold — горизонтальная пунктирная линия поверх текущего дня. Линия
       не «дёргает» лейаут: position:absolute + bottom:X% (X считается в
       _render_weekly_grid той же функцией, что высота бара). z-index 2 — чтобы
       быть над баром. На узких экранах подпись прячется, линия остаётся. */
    .threshold {{
      position: absolute; left: -3px; right: -3px;
      border-top: 2px dashed #f5f7fb;
      pointer-events: none; z-index: 2;
    }}
    .threshold-label {{
      position: absolute; left: 100%; top: -10px;
      margin-left: 6px; white-space: nowrap;
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 10px; font-weight: 700;
      color: #f5f7fb; text-transform: none; letter-spacing: 0;
    }}
    .days {{
      /* Абсолютно внизу карточки, поверх .bars. bottom:12px = .week padding-bottom.
         z-index:2 перекрывает бар, у которого 100K (height 0%) сидит ровно на
         верхней границе .days (= 89.08% .week = 100K-лейбл). margin-top:10px
         из старого лейаута не нужен — .bars сам отступает через bottom:38px. */
      position: absolute; bottom: 12px; left: 12px; right: 12px; z-index: 2;
      display: grid; grid-template-columns: repeat(7, 1fr); gap: 7px;
      margin: 0; padding-top: 10px; border-top: 1px solid var(--history-2);
      color: var(--muted); font-size: 12px; text-align: center;
    }}
    /* ===== Today · 24H Stream card =====
       Наследует .panel.chart-panel (border-radius, фон, padding) и .chart-head
       (eyebrow + title + meta). Своё: 24-колоночный грид, бары прижаты к низу,
       подписи часов под барами. */
    .chart-shell--24h {{
      position: relative;
      padding: 20px 18px 12px;
    }}
    .hours-24h {{
      display: grid; grid-template-columns: repeat(24, 1fr); gap: 4px;
    }}
    .hour-cell {{
      /* Каждая из 24 ячеек. Flex-column: бар (flex:1, растёт вверх) + лейбл
         (auto, по высоте строки). Лейбл в normal flow, не absolute, поэтому
         не торчит за пределы chart-shell и не прилипает к нижней рамке.
         Совпадает с .days в weekly chart: цвет и шрифт — var(--muted) Inter 12px,
         центрированы, идут одной строкой под барами.
         position:relative — якорь для .peak-value (absolute сверху). */
      position: relative;
      display: flex; flex-direction: column; justify-content: flex-end;
      gap: 6px; min-height: 152px;
    }}
    /* Token-count label над peak-баром (TL, 2026-08-05): показывает величину
       пикового часа прямо в карточке, не уходя в meta-строку. 5px сверху
       cell'а — пиковый бар упирается в верх (100% height), лейбл висит
       над ним; для не-пиковых ячеек лейбл не рендерится вообще. */
    .peak-value {{
      position: absolute; top: 5px; left: 0; right: 0;
      z-index: 1;
      text-align: center;
      color: #e6ebf6;
      font-size: 14px;
      letter-spacing: 0.02em;
      pointer-events: none;
    }}
    .bar-24h {{
      width: 100%; flex: 0 1 auto; align-self: end;
      border-radius: 6px 6px 2px 2px; min-height: 2px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.10);
      transition: filter 0.15s ease;
    }}
    .bar-24h:hover {{ filter: brightness(1.18); }}
    /* Single-shade palette (TL review, 2026-08-05): все бары с данными
       (active / peak / current) — один и тот же тёмно-зелёный #216e39.
       Величина читается только высотой; peak — самый высокий бар, без
       отдельного bright accent. L1..L3 шкала GitHub-палитры убрана
       как избыточная — она дублировала height-кодирование. */
    .bar-24h.active,
    .bar-24h.peak,
    .bar-24h.current {{
      background: #216e39;
    }}
    /* Peak больше не имеет bright accent — отличается от active только
       позицией в meta-строке карточки и тем, что это самый высокий бар. */
    .bar-24h.peak {{
      box-shadow: none;
    }}
    /* Current hour (h == now.hour): та же заливка, но с тонким контуром
       сверху, чтобы было видно «этот час ещё копит». */
    .bar-24h.current {{
      outline: 1px solid rgba(255,255,255,0.55);
      outline-offset: -1px;
    }}
    /* Future (h > now.hour): пунктир, opacity 0.55. min-height:6px даёт
       видимую "ячейку-заглушку", чтобы было ясно "здесь скоро появятся
       данные". */
    .bar-24h.future {{
      height: 6px !important;
      background: transparent;
      border: 1px dashed rgba(255,255,255,0.18);
      box-shadow: none; opacity: 0.55;
    }}
    /* Empty (h < now.hour, value=0): 2px нейтральный floor, как .bar.future в
       weekly. Маркирует "час прошёл, данных нет". */
    .bar-24h.empty {{
      background: rgba(255,255,255,0.06);
      box-shadow: none;
    }}
    .hour-label {{
      /* Как .days в weekly chart: var(--muted), Inter 12px, центрированы.
         flex:none — занимает ровно свою высоту, не растягивается. */
      flex: none; text-align: center;
      color: var(--muted);
      font-size: 12px; line-height: 1;
    }}
    .hour-label--future {{ opacity: 0.4; }}
    /* NOW-маркер удалён (TL review, 2026-08-04): на широкой карточке 24-баров
       бейдж "NOW" поверх текущего часа дублирует то, что и так читается из
       meta-строки ("Всего · Пик"). Текущий час по-прежнему визуально
       выделяется тонким outline (.bar-24h.current) — этого достаточно. */
    /* Легенда интенсивности "Less .... More" удалена (TL review, 2026-08-05):
       дублирует то, что читается из самих баров — empty/future через пунктир/2px
       floor, peak через отдельный яркий accent, current через outline, активные
       часы через шкалу L1..L4 (GitHub-палитра) с прогрессией яркости от L1 к L4.
       Палитра L1..L4+PEAK остаётся как семантика интенсивности, явный блок
       "Less/More" + 4 swatch'а убран как шум. */
    @media (max-width: 980px) {{
      .kpis, .weeks {{ grid-template-columns: 1fr; }}
      .chart-head, .hero-top {{ flex-direction: column; align-items: flex-start; }}
      .plot {{ grid-template-columns: 1fr; }}
      .axis {{ display: none; }}
      /* На узком лейауте подпись threshold'а прячем, линия остаётся. */
      .threshold-label {{ display: none; }}
      /* 24-часовой грид на узком экране: 12 колонок × 2 ряда часов (если совсем
         тесно — перестроить на 8×3). Для MVP оставляем 24×1 с уменьшенной
         минимальной шириной через .hour-cell, чтобы грид схлопнулся до
         горизонтальной прокрутки на совсем узких экранах. */
      .hours-24h {{ grid-template-columns: repeat(24, minmax(14px, 1fr)); }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <div class="topbar">
      <span>Token Dashboard</span>
      <span>Обновлено: {today_label}</span>
    </div>
    <section class="hero">
      <article class="panel hero-card">
        <div class="hero-top">
          <div>
            <p class="eyebrow">Today Token Usage</p>
          </div>
          {hero_pills}
          <div class="tz-chip">MSK (UTC+3)</div>
        </div>
        <section class="kpis">
          <article class="panel kpi kpi--yellow">
            <div class="kpi-head">
              <div>
                <div class="kpi-title">Текущий момент</div>
                <div class="kpi-time">{cur_time}</div>
              </div>
            </div>
            <div class="kpi-value">{fmt_tokens(current_hour_tokens)}</div>
          </article>
          <article class="panel kpi kpi--blue">
            <div class="kpi-head">
              <div>
                <div class="kpi-title">Сегодня</div>
                <div class="kpi-time">{today_time}</div>
              </div>
            </div>
            <div class="kpi-value">{fmt_tokens(today_tokens)}</div>
          </article>
          <article class="panel kpi kpi--green">
            <div class="kpi-head">
              <div>
                <div class="kpi-title">Рабочее время</div>
                <div class="kpi-time">{window_time}</div>
              </div>
            </div>
            <div class="kpi-value">{fmt_tokens(window_total)}</div>
          </article>
          <article class="panel kpi kpi--orange">
            <div class="kpi-head">
              <div>
                <div class="kpi-title">Активность</div>
                <div class="kpi-time">{today_time}</div>
              </div>
              <div class="kpi-pill" title="Средний расход токенов (input+output) на одну сессию сегодня">
                ≈ <strong>{fmt_tokens(avg_tokens_per_session)}</strong>/сессию
              </div>
            </div>
            <div class="kpi-fraction">
              <div class="kpi-value kpi-fraction__value">{fmt_avg(today_avg)}</div>
              <div class="kpi-fraction__stack">
                <span class="kpi-fraction__num">{today_sessions}</span>
                <span class="kpi-fraction__divider"></span>
                <span class="kpi-fraction__den">{today_user_requests}</span>
              </div>
            </div>
          </article>
        </section>
      </article>
    </section>
    <section class="panel chart-panel" id="chart-scale" data-scale="log">
      <div class="chart-head">
        <p class="eyebrow">Weekly Compare</p>
        <div class="chart-meta">
          <span class="week-cap-pill" title="Недельный лимит расхода токенов">Порог недели: <strong>{fmt_tokens(WEEKLY_CAP_TOKENS)}</strong></span>
          <span class="chart-meta__variant chart-meta__linear">
            Шкала: 0 … {fmt_int(y_max)} токенов
          </span>
          <span class="chart-meta__variant chart-meta__log">
            Шкала: log, {log_meta_range} токенов
          </span>
          <div class="scale-toggle" role="tablist" aria-label="Шкала weekly chart">
            <button type="button" class="scale-toggle__btn" data-scale="linear" role="tab">Линейная</button>
            <button type="button" class="scale-toggle__btn is-active" data-scale="log" role="tab" aria-selected="true">Log</button>
          </div>
        </div>
      </div>
      <div class="chart-shell">
        <div class="plot chart-variant chart-variant--linear">
          <div class="axis">{axis_linear}</div>
          <div class="weeks">{linear_grid}</div>
        </div>
        <div class="plot chart-variant chart-variant--log">
          <div class="axis">{axis_log}</div>
          <div class="weeks">{log_grid}</div>
        </div>
      </div>
    </section>
    <section class="panel chart-panel chart-panel--24h">
      <div class="chart-head">
        <p class="eyebrow">Today · 24H Stream <span class="eyebrow__date">{today_date}</span></p>
        <div class="chart-meta">
          Всего: <strong>{fmt_tokens(stream_total)}</strong> · {peak_meta}
        </div>
      </div>
      {_render_24h_stream(today_24h, stream_total)}
    </section>
  </main>
  <script>
    // Linear/log toggle для weekly chart.
    // Приоритет источников состояния (от сильного к слабому):
    //   1. ?scale=log|linear в URL — для шаринга ссылок.
    //   2. localStorage[tokenDashboardScale] — переживает ребилд dashboard.html.
    //   3. 'log' по умолчанию (как в concept-ops, активна «Log»).
    (function () {{
      var STORAGE_KEY = 'tokenDashboardScale';
      var root = document.getElementById('chart-scale');
      if (!root) return;
      function readStored() {{
        try {{ return localStorage.getItem(STORAGE_KEY); }}
        catch (e) {{ return null; }}
      }}
      function writeStored(scale) {{
        try {{ localStorage.setItem(STORAGE_KEY, scale); }}
        catch (e) {{ /* silent — URL всё ещё работает */ }}
      }}
      function normalize(s) {{ return (s === 'log' || s === 'linear') ? s : null; }}

      var urlScale = normalize(new URLSearchParams(window.location.search).get('scale'));
      var storedScale = normalize(readStored());
      var initial = urlScale || storedScale || 'log';
      if (urlScale) writeStored(urlScale);

      apply(initial, false);
      root.querySelectorAll('.scale-toggle__btn').forEach(function (btn) {{
        btn.addEventListener('click', function () {{ apply(btn.dataset.scale, true); }});
      }});
      function apply(scale, persist) {{
        root.dataset.scale = scale;
        root.querySelectorAll('.scale-toggle__btn').forEach(function (b) {{
          var active = b.dataset.scale === scale;
          b.classList.toggle('is-active', active);
          b.setAttribute('aria-selected', active ? 'true' : 'false');
        }});
        if (persist) {{
          var url = new URL(window.location);
          if (scale === 'linear') url.searchParams.delete('scale');
          else url.searchParams.set('scale', scale);
          history.replaceState(null, '', url);
          writeStored(scale);
        }}
      }}
    }})();
  </script>
</body>
</html>
"""


