# ADR-001 — Экспонировать закэшированные токены (cache_read / cache_write) в API и дашбордах

- **Status:** proposed
- **Date:** 2026-08-22
- **Deciders:** Senior Tech Lead
- **Type:** feature (observability / metric exposure)

---

## 1. Context

Агент пишет в `local_runtime_token_usage` не только `input_tokens` /
`output_tokens`, но и поля о работе с промпт-кэшем:
`cache_read_tokens` и `cache_write_tokens` (плюс те же значения в
JSON-колонке `raw` как `cacheRead` / `cacheWrite`).

Текущий сервис **агрегирует и отдаёт клиенту только** `input + output`:

- `services/sessions.py::_tokens_per_session` — суммирует только
  `input_tokens` и `output_tokens`;
- модели `models/sessions.py::SessionRow` (и аналогичные aggregate DTO)
  не содержат cache-полей;
- `README.md` явно фиксирует: *"`cache_read_tokens`,
  `cache_write_tokens`, `reasoning_tokens` are excluded from the primary
  metric"*.

То есть данные о закэшированных токенах **есть в исходной БД, но не
доходят до дашбордов** — теряются на этапе агрегации и DTO.

### 1.1 Реальные данные по активной сессии (справочные для кодера)

**Активная (последняя) сессия:**

| Поле | Значение |
|---|---|
| `session_id` | `mvs_648e150f65d144e1ae03e143880dac4b` |
| `agent_name` | `mavis` |
| `framework_type` | `pi-agent` |
| `model` | `minimax/MiniMax-M3` |
| `workspace_dir` | `C:/Projects/Common/0830_agent-registry` |
| `title` | `Phase 05` |
| `status` | `started` (active) |
| `updated_at_ms` | `1788092938323` |
| записей `token_usage` | `174` |

**Суммарно по сессии (по всей таблице `token_usage WHERE session_id=...`):**

| Метрика | Значение |
|---|---|
| `input_tokens` | `526 295` |
| `output_tokens` | `73 314` |
| `reasoning_tokens` | `0` |
| `cache_read_tokens` | **`18 221 319`** |
| `cache_write_tokens` | **`0`** |
| `cost_usd` (billing-колонка) | `0.0` |
| строк с `cache_read_tokens > 0` | `174` (из 174 — все) |
| строк с `cache_write_tokens > 0` | `0` (из 174) |

Сводка по активной сессии: **cache-read огромен** (~18.2M) и на 2 порядка
превышает `input` (0.53M) — значит основной вклад в потребление это как
раз **чтение из кэша**, которое сейчас дашборд не показывает вовсе.

**Последние записи `token_usage` активной сессии (что реально лежит в БД):**

| id | ts | input | output | cacheRead | cacheWrite | totalTokens | raw |
|---|---|---|---|---|---|---|---|
| 30095 | 1788092984363 | 298 | 1020 | 180576 | 0 | 181894 | `{"input":298,"output":1020,"cacheRead":180576,"cacheWrite":0,"totalTokens":181894,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}}` |
| 30094 | 1788092941957 | 1508 | 2604 | 176479 | 0 | 180591 | `{"input":1508,"output":2604,"cacheRead":176479,"cacheWrite":0,"totalTokens":180591,"cost":{...0}}` |
| 30093 | 1788092790645 | 332 | 2436 | 173726 | 0 | 176494 | `{"input":332,"output":2436,"cacheRead":173726,"cacheWrite":0,"totalTokens":176494,"cost":{...0}}` |

Ключевая деталь для кодера: в `raw` поле `totalTokens` уже включает
`cacheRead` (**`total = input + output + cacheRead + cacheWrite`**), а
`cost.cacheRead` / `cost.cacheWrite` сейчас равны `0` в данных агента.

### 1.2 Схема таблицы (реальная)

```sql
local_runtime_token_usage (
    id                INTEGER,
    session_id        TEXT,
    agent_name        TEXT,
    framework_type    TEXT,
    turn_id           TEXT,
    model             TEXT,
    ts                INTEGER,     -- Unix epoch ms
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    reasoning_tokens  INTEGER,
    cache_read_tokens INTEGER,     -- <-- есть, не используется сервисом
    cache_write_tokens INTEGER,    -- <-- есть, не используется сервисом
    cost_usd          REAL,
    raw               TEXT         -- JSON c cacheRead/cacheWrite/totalTokens/cost
)
```

> Примечание: `docs/API_CONTRACT.md` §"DB schema" на данный момент
> описывает только 3 колонки (`session_id`, `ts`, `input_tokens`,
> `output_tokens`) — документация отстала от реальной схемы. Обновить
> заодно (см. §5).

## 2. Decision

Экспонировать закэшированные токены как **дополнительные метрики**
(не меняя существующую primary metric `input + output`):

1. В каждом агрегате (per-session, per-project, per-hour/day bucket,
   данное окно, активная сессия) добавить `cache_read_tokens` и
   `cache_write_tokens` как отдельные численные поля.
2. **`tokens` / `input_tokens` / `output_tokens` — не трогаем** (обратная
   совместимость, дашборды не ломаются, соседние тесты зелёные).
3. В DTO добавить отдельные поля `cache_read_tokens`,
   `cache_write_tokens` и, опционально, `cache_total` / включить cache в
   `total` **только как отдельное поле** (не менять смысл `tokens`).
4. Рассчитать `cost_usd` для кэша из конфигурируемой цены за 1M токенов
   (новая env-переменная, см. §3.2) — по аналогии с input/output.
5. Показывать cache-метрики в дашбордах: активная сессия + строка
   сессии/проекта.

### 2.1 Альтернативы, отклонённые

| Альтернатива | Почему отклонена |
|---|---|
| Включить cache в существующее поле `tokens` | ломает инвариант `tokens == input + output` у клиента и в тестах; ломает сравнение с прежними данными |
| Только `cache_total = input + output + cacheRead + cacheWrite` из `raw.totalTokens` | теряется разделение read/write; противоречит цели фичи (показать именно кэш) |
| Ничего не делать | теряется ~97% фактического потребления (18.2M vs 0.6M) активной сессии — данные есть, но невидимы |
| Писать `cache_*` в `raw` без колонок | колонки уже есть в БД; использовать их напрямую проще и надёжнее, чем парсить JSON |

## 3. Implementation touch points (для кодера)

### 3.1 SQL-агрегации

Во всех местах, где сейчас `SUM(input_tokens)`, `SUM(output_tokens)`,
добавить:

```sql
COALESCE(SUM(cache_read_tokens),  0) AS cache_read_tokens,
COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
```

Пример (приоритет — активная сессия и `sessions`):
- `services/sessions.py::_tokens_per_session` — главный кандидат;
- `services/tokens.py` (today/hourly/window/active session);
- `services/projects.py`, `services/project.py`.

### 3.2 Новые env-переменные (config.py)

```python
cost_cache_read_per_1m_usd: float = 0.06   # $0,06 / 1M токенов (утверждено)
cost_cache_write_per_1m_usd: float = 0.0
```

У агента в `raw` сейчас `cost.cacheRead = cost.cacheWrite = 0.0` — цену
кэша задаём конфигом, как сделано для input/output. **Утверждённое
значение для cache read — `$0,06 / 1M токенов`** (env:
`AGENTDASH_COST_CACHE_READ_PER_1M_USD=0.06`). Для cache write цена пока
не определена (в данных = 0) — оставляем дефолт `0.0` до появления
реальных значений.

### 3.3 Модели (models/*.py)

Добавить поля, не ломая существующие:

```python
cache_read_tokens: int = 0
cache_write_tokens: int = 0
```

Куда добавить: `SessionRow` (sessions.py), соответствующие DTO в
`tokens.py`, `projects.py`, `project.py`. Для компактности можно вынести
cache-тройку в общий под-объект (например `cache: {read, write, cost}`),
если не хочется засорять плоские поля.

### 3.4 Дашборды (dashboard.html / project-dashboard.html / session-dashboard.html)

Добавить вывод cache_read/cache_write (и cost) в активную сессию и в
строки сессий/проектов. Показывать как отдельную метрику, рядом с
input/output.

### 3.5 Документация

- Обновить `docs/API_CONTRACT.md` §"DB schema" (реальная схема — см.
  §1.2) и shapes ответов `tokens/snapshot`, `sessions/snapshot`,
  `projects/snapshot`.
- Обновить `docs/ARCHITECTURE.md` при необходимости (метрики/слои).
- Обновить `README.md` §"Metric" (раздел про исключение cache-полей).
- Дополнить `CHANGELOG.md`.

## 4. Consequences

### Positive

- Дашборд начинает отражать **реальное** потребление: в активной сессии
  cache-read (18.2M) на 2 порядка больше input (0.5M) — это главная
  цифра, которую сейчас не видно.
- Совместимость: `tokens == input + output` не меняется, существующие
  тесты и клиент не ломаются.
- Цена кэша конфигурируема и по умолчанию `0` — безопасный rollout.

### Negative

- Больше полей в JSON и больше данных в дашбордах (нужно аккуратно
  расположить в UI, не перегружая карточку).
- Риск рассинхрона: если агент начнёт заполнять `cache_write_tokens`
  (сейчас = 0), значения появятся — проверить, что DTO/UI это
  поддерживают с самого начала.

### Neutral

- `reasoning_tokens` пока остаётся вне scope (в данных = 0); при желании
  добавить тем же механизмом позже.

## 5. Reference — формулы и данные для проверки

Ожидаемое поведение после имплементации для активной сессии
`mvs_648e150f65d144e1ae03e143880dac4b`:

```
tokens            = input + output            = 526295 + 73314            = 599609
cache_read_tokens = SUM(cache_read_tokens)    = 18221319
cache_write_tokens= SUM(cache_write_tokens)   = 0
```

Стоимость кэша (при `AGENTDASH_COST_CACHE_READ_PER_1M_USD=0.06`):

```
cache_read_cost_usd = cache_read_tokens / 1_000_000 * 0.06
                    = 18_221_319 / 1_000_000 * 0.06 ≈ $1.09
```

Проверочный SQL (pytest/ручной):

```sql
SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens),
       SUM(cache_read_tokens), SUM(cache_write_tokens)
FROM local_runtime_token_usage
WHERE session_id = 'mvs_648e150f65d144e1ae03e143880dac4b';
-- 174, 526295, 73314, 18221319, 0
```
