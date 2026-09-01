# ADR-002 — Связать выбор дня в WEEKLY COMPARE с 24H STREAM (drilldown)

- **Status:** proposed
- **Date:** 2026-09-01
- **Deciders:** Senior Tech Lead
- **Type:** feature (UI / API surface)

---

## 1. Context

`dashboard.html` рендерит два независимых графика:

- **WEEKLY COMPARE** — 4 недели × 7 дней, бары по дням. Сейчас чисто
  визуальный, без интерактива.
- **24H STREAM** — 24 часовых бара **только за сегодня**. Источник
  `data.today.hourly` в snapshot-ответе. Заголовок `TODAY · 24H STREAM`.

Бэкенд уже агрегирует **все** `(date, hour)` корзины за rolling-окно
(`_aggregate_by_hour_split` в `services/tokens.py` берёт данные от
Monday `week_count` недель назад), но в ответе экспонирует только
`today.hourly` — 24 бара. Часовые данные прошлых дней лежат в
`hourly_map` и используются только для суммирования в `_build_weekly_block`.

**Что хотим:** клик по любому дневному бару в WEEKLY COMPARE → подтягивает
часовые данные этого дня в 24H STREAM. Дефолт — сегодня. Выбор
персистится в `localStorage` и сбрасывается на следующий календарный
день (в `Europe/Moscow`).

**Что это НЕ:** это не drilldown в сессии/проекты, не фильтр по модели
или агенту, не новый эндпоинт. Это UI-связка двух уже существующих
блоков + минимальное API-расширение для экспонирования часовых
корзин за прошлые дни.

### 1.1 Скриншот текущего состояния

На W-36 (current week) виден бар «Вт» с красной рамкой — это место,
где сейчас сидит «сегодня» (2026-09-01, Вт). Цель: заменить красную
рамку на белую обводку 1px и сделать её кликабельной; нижний график
должен зависеть от того, на каком дне она сейчас.

### 1.2 Граничные условия

| Случай | Поведение |
|---|---|
| LS пуст, открыли страницу | выбран = today, обводка на сегодня, stream = сегодня |
| LS = today, открыли страницу | то же, что выше (идемпотентно) |
| LS = вчера (устарел), открыли страницу | LS очищается, выбран = today |
| LS > today (теоретически) | LS очищается, выбран = today |
| Клик по дню из прошлой недели | переключаем stream, пишем LS, двигаем обводку |
| Клик по сегодняшнему дню, когда выбран не сегодня | то же: переключаем на today, пишем LS |
| Клик по уже выбранному дню | **no-op** (по решению TL, 2026-09-01) |
| Клик по `null`-дню (future / no-data) | кнопка не активна, курсор не pointer, клик игнорируется |
| Страница открыта переходом через полночь MSK | сброс только при следующем F5 / открытии (без setInterval, по решению TL) |
| Polling раз в 5 мин при смене дня | на следующем fetch новый `data.today.date` ≠ старый → сброс LS, выбран = today |

## 2. Decision

### 2.1 Storage: `localStorage` с ключом `agentdash:selectedDay`

- Значение: ISO-дата `"YYYY-MM-DD"` (та же, что и `data.today.date`).
- TTL: не нужно (см. rollover-логику в init).
- Почему не `sessionStorage`: пользователь явно просил пережить
  закрытие вкладки. Почему не URL hash: лишний шум в адресной строке
  для локального read-only дашборда.

### 2.2 API: один новый блок в существующем snapshot, без новых эндпоинтов

Добавить в `TokensSnapshot` поле `hourly_by_date: dict[str, list[HourlyBar]]`,
где ключ — `"YYYY-MM-DD"`, значение — массив из **ровно 24** `HourlyBar`
для каждого дня в rolling-окне (включая дни без данных — нулевые бары).
Клиент ищет `hourly_by_date[selectedDate]` и рендерит 24H STREAM.

**Почему так, а не альтернативы:**

| Альтернатива | Почему отклонена |
|---|---|
| Новый эндпоинт `GET /api/v1/tokens/hourly?date=...` | удваивает roundtrips; `_aggregate_by_hour_split` уже всё считает за один SQL; несовместимо с пятой конвенцией «один snapshot = один запрос» |
| Вложить 24 часа в `WeeklyDay` | раздувает каждый день; дублирует данные, которые и так есть в `weekly.weeks[].days[]` (агрегат по дню) и в `hourly_by_date` (почасовая разбивка) |
| Не отдавать почасовку, рендерить из агрегатов | потеря детализации: пик часа, is_current, intensity — нужны для 24H рендера |
| URL hash / query `?date=` | см. §2.1 — лишний шум; пользователь явно попросил localStorage |

**Размер payload:** 5 недель × 7 дней × 24 часа × ~6 полей ≈ 8 КБ JSON
дополнительно. На loopback-сервисе с 5-мин polling это ничто.

### 2.3 UI: белый outline 1px на `.bar-cell`, клик переключает stream

- CSS-класс `.bar-cell--selected` добавляет
  `outline: 1px solid rgba(255,255,255,0.85); outline-offset: -1px;`
  к самому бару (не к `.bar-cell`, иначе прыгает layout).
- Применяется ровно к одному `.bar-cell` — дню, который сейчас
  показан в 24H STREAM. Дефолт — сегодня.
- Курсор `pointer` ставится на `.bar-cell`, у которых есть данные
  (`day !== null` И `day.date <= today.date`). На `null`-дни
  (future / no-data) клик не вешается.
- No-op при клике по уже выбранному дню (по решению TL).
- Заголовок 24H STREAM: `24H STREAM` (без `Today` — он не всегда
  today). Дата в `eyebrow__date` = `selectedDate`.

### 2.4 Stream-рендер для НЕ-сегодняшнего дня

Когда `selectedDate < today.date`:

- Все 24 часа — «прошедшие»: `is_future = false`, `is_current = false`.
- `is_current` подсвечивается только если `selectedDate === today.date`.
- Peak-метка показывается как обычно (по выбранному дню).
- «Будущих» часов нет, логика future-dashed не применяется.

Когда `selectedDate === today.date`:

- Поведение идентично текущему (currentHour подсвечен, future = dashed).

### 2.5 Что НЕ меняется

- KPI-карточки (1–4), hero-pills, day-pill, weekly totals — **остаются
  привязаны к today**. Selection не каскадирует на них — иначе это уже
  не «связка двух блоков», а «фильтр всего дашборда» (другая фича, под
  другой ADR).
- `data.now_session`, `data.sparklines`, `data.weekly` — без изменений.
- `services/tokens.py::_aggregate_by_hour_split` — **не меняем**
  (уже возвращает все `(date, hour)` корзины). Добавляем отдельный
  builder, который превращает `hourly_map` в `hourly_by_date`.

## 3. Implementation touch points (для кодера)

### 3.1 Backend

**`src/agentdash_service/models/tokens.py`** — добавить Pydantic-модель:

```python
class HourlyByDate(BaseModel):
    dates: dict[str, list[HourlyBar]]  # {"YYYY-MM-DD": [24 bars], ...}
```

(или просто `dict[str, list[HourlyBar]]` инлайном в `TokensSnapshot` —
выбор по объёму; **рекомендация: инлайном**, чтобы не плодить
одноразовый под-DTO.)

В `TokensSnapshot` добавить:

```python
hourly_by_date: dict[str, list[HourlyBar]]
```

**`src/agentdash_service/services/tokens.py`** — новый builder:

```python
def _build_hourly_by_date(
    today: date,
    week_count: int,
    hourly_map: dict[tuple[date, int], tuple[int, int, int]],
) -> dict[str, list[HourlyBar]]:
    """Вернуть 24-часовые корзины для всех дат в rolling-окне.

    Для дат без данных — массив из 24 нулевых баров с intensity="L0".
    Выходит за пределы week_count ровно на текущую неделю, т.е.
    (week_count+1) × 7 дат максимум.
    """
    start_monday = today - timedelta(days=today.isocalendar()[2] - 1 + 7 * week_count)
    out: dict[str, list[HourlyBar]] = {}
    for offset in range((week_count + 1) * 7):
        d = start_monday + timedelta(days=offset)
        bars: list[dict[str, Any]] = []
        for h in range(24):
            in_t, out_t, _ = hourly_map.get((d, h), (0, 0, 0))
            total = in_t + out_t
            bars.append({
                "hour": h,
                "input": in_t,
                "output": out_t,
                "total": total,
                "cost_usd": compute_cost(in_t, out_t, settings),
                "intensity": "L0",  # пересчитаем ниже через _compute_intensity
                "is_current": False,
                "is_future": d > today,
                "is_empty": total == 0,
            })
        _compute_intensity(bars)
        out[d.isoformat()] = [HourlyBar(**b) for b in bars]
    return out
```

⚠️ `is_current = (d == today and h == current_hour)` — нужно
прокинуть `now: datetime` в builder (по аналогии с `_build_today_block`).

**В `build_snapshot`** добавить вызов и пробросить результат в конструктор
`TokensSnapshot`.

### 3.2 Frontend (`dashboard.html`)

**CSS** (в блок `<style>`):

```css
.bar-cell--selected .bar {
  outline: 1px solid rgba(255,255,255,0.85);
  outline-offset: -1px;
}
.bar-cell--clickable { cursor: pointer; }
```

**JS** — новый модуль выбора дня, рядом с scale-toggle IIFE:

```js
const SELECTED_DAY_KEY = "agentdash:selectedDay";

function readSelectedDay() {
  try { return localStorage.getItem(SELECTED_DAY_KEY); } catch (e) { return null; }
}
function writeSelectedDay(d) {
  try { localStorage.setItem(SELECTED_DAY_KEY, d); } catch (e) {}
}
function clearSelectedDay() {
  try { localStorage.removeItem(SELECTED_DAY_KEY); } catch (e) {}
}
function resolveSelectedDay(serverToday) {
  // serverToday = "YYYY-MM-DD" (data.today.date)
  const stored = readSelectedDay();
  if (!stored) return serverToday;
  if (stored === serverToday) return serverToday;
  // stale or invalid → reset
  clearSelectedDay();
  return serverToday;
}

let selectedDay = null;  // инициализируется в render() после первого fetch

function isClickableDay(day, todayStr) {
  return day !== null && day.date <= todayStr;
}

function applySelectedDayOutline() {
  document.querySelectorAll(".bar-cell--selected").forEach(n =>
    n.classList.remove("bar-cell--selected"));
  if (!selectedDay) return;
  document.querySelectorAll(`.bar-cell[data-date="${selectedDay}"]`)
    .forEach(n => n.classList.add("bar-cell--selected"));
}

function selectDay(dateStr) {
  if (dateStr === selectedDay) return;  // no-op
  selectedDay = dateStr;
  writeSelectedDay(dateStr);
  applySelectedDayOutline();
  renderStreamForSelected();
}
```

**`setDayBar`** — добавить `data-date` атрибут на `.bar-cell` и
класс `bar-cell--clickable` когда день кликабельный:

```js
function setDayBar(cell, day, idxInWeek, isCurrentWeek, logMode, yMax, todayStr) {
  // ... existing logic ...
  if (day) {
    cell.dataset.date = day.date;
    if (isClickableDay(day, todayStr)) cell.classList.add("bar-cell--clickable");
  }
}
```

**`renderWeek`** — прокинуть `todayStr` в `setDayBar` и повесить
обработчик `click` после `setDayBar`:

```js
const cell = document.createElement("div");
cell.className = "bar-cell";
setDayBar(cell, day, i, week.is_current, logMode, yMax, todayStr);
if (day && isClickableDay(day, todayStr)) {
  cell.addEventListener("click", () => selectDay(day.date));
}
bars.appendChild(cell);
```

**`renderHours`** — переименовать в `renderStreamForSelected(bars, isToday, currentHour)`,
принимает уже готовые бары. `isCurrent = b.hour === currentHour` срабатывает
только при `isToday === true`. Иначе все часа — обычные.

**`render(data)`** — в конце:

```js
selectedDay = resolveSelectedDay(data.today.date);
applySelectedDayOutline();
renderStreamForSelected(
  data.hourly_by_date[selectedDay] || [],
  selectedDay === data.today.date,
  curHour,
);
el("eyebrow-date").textContent = selectedDay;
```

(вместо текущих двух строк про `eyebrow-date` и `renderHours`).

**HTML** — поменять eyebrow:

```html
<p class="eyebrow">24H Stream <span class="eyebrow__date" id="eyebrow-date">—</span></p>
```

### 3.3 Тесты

**`tests/test_tokens_snapshot.py`** — расширить `test_tokens_snapshot_shape`:

```python
assert "hourly_by_date" in data
assert isinstance(data["hourly_by_date"], dict)
# Минимум 5 weeks × 7 days = 35 дат
assert len(data["hourly_by_date"]) >= 5 * 7
# Каждая дата → ровно 24 бара
for date_str, bars in data["hourly_by_date"].items():
    assert len(bars) == 24, f"{date_str} has {len(bars)} bars"
    for b in bars:
        assert 0 <= b["hour"] <= 23
        assert b["total"] == b["input"] + b["output"]
```

**`docs/API_CONTRACT.md`** — добавить `hourly_by_date` в описание
`TokensSnapshot` (сразу после `sparklines` или после `today`).

**`CHANGELOG.md`** — запись в `[Unreleased]`:

```markdown
### Added
- **Drilldown: выбор дня в WEEKLY COMPARE → 24H STREAM.** Клик по
  дневному бару в любой неделе переключает нижний график на часовые
  данные этого дня. Дефолт — сегодня. Выбор персистится в
  `localStorage` под ключом `agentdash:selectedDay`, сбрасывается
  на следующий календарный день. На выбранном дне в weekly — белая
  обводка 1px. API: новый блок `hourly_by_date` в
  `GET /api/v1/tokens/snapshot` (24 бара × все даты rolling-окна).
```

## 4. Consequences

### Positive

- Drilldown из weekly в daily за 0 roundtrips — данные уже считаются.
- LS-персист переживает F5 / закрытие вкладки, сбрасывается на
  следующий день (не «вечно помнит»).
- API-расширение обратно совместимо: `hourly_by_date` — новое поле,
  старые клиенты игнорируют. `data.today.hourly` остаётся как есть.
- Тесты растут линейно (один новый shape-assert), без новых фикстур.

### Negative

- Payload snapshot вырастает на ~8 КБ. На 5-мин polling это 0.027 КБ/с
  — незаметно.
- «Будущие» дни в `hourly_by_date` — нулевые бары с `is_future=true`.
  Клиент НЕ должен по ним кликать, но они там есть, потому что
  бэкенд-логика окон проще, чем UI-логика выбора.
- Frontend и backend должны договориться о TZ-семантике `today`:
  backend даёт `data.today.date` в `Europe/Moscow`, frontend
  сравнивает LS ровно с этой строкой. Браузерный TZ не участвует.

### Neutral

- KPI / hero / day-pill остаются на today. Если позже захочется
  «фильтровать всё по выбранному дню» — это отдельный ADR.
- Roll-over через полночь — без таймера, на F5. По решению TL.

## 5. Reference — данные для проверки

### 5.1 SQL для ручной проверки (активные данные)

```sql
-- Часовые корзины за 2026-08-31 (любой день в окне)
SELECT strftime('%H', ts / 1000, 'unixepoch', '+3 hours') AS h,
       SUM(input_tokens) AS in_t, SUM(output_tokens) AS out_t
FROM local_runtime_token_usage
WHERE ts >= CAST(strftime('%s', '2026-08-31 00:00:00', '+3 hours') AS INT) * 1000
  AND ts <  CAST(strftime('%s', '2026-09-01 00:00:00', '+3 hours') AS INT) * 1000
GROUP BY h
ORDER BY h;
```

### 5.2 UI smoke test (после реализации)

1. Открыть `dashboard.html`, проверить: 24H STREAM = сегодня,
   обводка на «Вт» W-36.
2. Кликнуть «Чт» W-35 → нижний график перерисовывается под этот день,
   обводка переезжает, в eyebrow новая дата.
3. F5 → состояние сохранилось.
4. В DevTools `localStorage.setItem("agentdash:selectedDay", "2026-01-01")`,
   F5 → LS сброшен, обводка вернулась на сегодня.
5. Закрыть вкладку, открыть снова → выбранный день сохранился.
6. Кликнуть по «Ср» (которое уже выбрано) → ничего не происходит.
7. Кликнуть по future-дню (за пределами W-36 «Вс» не существует в окне,
   но null-ячейка) → клик игнорируется.

### 5.3 Ожидаемые значения в payload

```json
{
  "hourly_by_date": {
    "2026-08-25": [<24 bars, дни до rolling-окна>],
    "2026-08-26": [...],
    "...": "...",
    "2026-09-01": [<24 бара текущего дня>],
    "2026-09-02": [<24 нулевых бара, is_future=true>],
    "...": "..."
  }
}
```
