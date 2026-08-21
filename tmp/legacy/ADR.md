# Architecture Decision Records — Repo-Pulse Lite

## ADR-0000 — Design Philosophy

**Status:** Accepted

**Decision**

Repo-Pulse Lite optimizes for simplicity, not extensibility.

When simplicity and extensibility conflict, simplicity always wins.

This is the constitution of the project. Every other ADR is a consequence of this one; if a future decision contradicts it, this ADR wins by default.

---

## ADR-0001 — One Process

**Status:** Accepted

**Decision**

Весь pipeline выполняется одной командой:

```
snapshot → SQLite → HTML
```

Никаких сервисов.

**Consequences**

Плюсы:

- минимум кода
- нет daemon
- нет background jobs

Минусы:

- нет always-on dashboard

---

## ADR-0002 — SQLite is Source of Truth

**Status:** Accepted

**Decision**

SQLite хранит всю историю. Никаких CSV. Все отчёты читают SQLite.

---

## ADR-0003 — HTML instead of Web App

**Status:** Accepted

**Decision**

Вместо FastAPI — статический `report.html`, который открывается браузером.

**Причины**

- нет сервера
- нет шаблонизатора
- нет deployment

---

## ADR-0004 — Flat Modules

**Status:** Accepted

**Decision**

Запрещается создавать дополнительные слои. Разрешены только:

```
main.py
github.py
db.py
report.py
config.py
```

Новые файлы требуют отдельного ADR.

**Усиление**

Каждый новый Python-модуль обязан отвечать за отдельную ответственность, которую нельзя естественно разместить в существующих модулях.

Запрещены модули-свалки: `utils.py`, `helpers.py`, `common.py`, `models.py`. Если код не находит естественного места среди существующих пяти файлов — это сигнал пересмотреть решение, а не создавать шестой файл.

---

## ADR-0005 — No Future Architecture

**Status:** Accepted

**Decision**

Любой код должен отвечать на вопрос: _используется ли это сегодня?_

Если ответ «нет» — код не принимается.

---

## ADR-0006 — Complexity Budget

**Status:** Accepted

**Decision**

Каждое решение, добавляющее сложность, оценивается неформально по ориентировочной шкале:

```
новый файл                = +1
новая dependency           = +2
новый background process   = +10
новый web framework        = +20
```

Перед добавлением сложности нужно ответить: какую пользовательскую ценность она приносит?

**Важно**

Это эвристика для обсуждения на code review, а не формальная метрика с дашбордом или CI-гейтом — сама система подсчёта не должна превратиться в дополнительный процесс (см. ADR-0005). Единственная цель — сделать цену решения explicit до того, как оно принято.

---

## ADR-0007 — Dependency Policy

**Status:** Accepted

**Decision**

Dependencies are added only when they replace substantial custom code or provide essential functionality. Standard Library is preferred whenever practical.

**Consequences**

Плюсы:

- меньше проект
- быстрее запуск
- проще апгрейды
- ниже стоимость сопровождения

Минусы:

- иногда придётся писать чуть больше кода вручную вместо готового пакета

---

## ADR-0008 — Per-Row Expandable Activity Block

**Status:** Draft

**Decision**

Активность проекта в дашборде `report.html` показывается **не под таблицей проектов, а под выбранной строкой**.

Структура:

- вся строка таблицы кликабельна — toggle expand/collapse
- по умолчанию accordion (один проект открыт); `Shift+клик` — открыть ещё один без закрытия текущего
- состояние (открыт/закрыт + выбранный день) хранится в `localStorage`, ключ — `project_path`
- свёрнутое состояние не показывает ничего — проект в списке = данные есть
- footer (`22 PROJECTS · 172 SESSIONS · 5 WEEKS (W-30..W-34)`, `149.05M TOKENS`) остаётся глобальным

Layout блока:

- `N дней ≤ 7` → side-by-side: дневной чарт 30% слева, часовой 70% справа
- `N дней > 7` → stacked (как было): дневной сверху, часовой снизу
- целевая высота блока ~180–200px

Range (`1Д / НЕДЕЛЯ / 5 НЕДЕЛЬ`) и drill-down `день → 24h` остаются **per-project**, не глобальные — ритм активности у разных проектов разный.

**Причины**

- визуальная близость: данные прямо под проектом, не надо скроллить
- side-by-side даёт прямое соответствие `выбранный день ↔ его часы` без отдельного клика
- больше проектов остаётся выше fold в свёрнутом состоянии
- collapsed без sparkline — минимум визуального шума

**Consequences**

Плюсы:

- дашборд остаётся одним `report.html` без сервера (ADR-0003)
- правки ложатся в существующий `report.py` — новых модулей не нужно (ADR-0004)
- ванильный JS — без зависимостей (ADR-0007)
- сложность ниже бюджета: 0 файлов, 0 deps, 0 процессов (ADR-0006)

Минусы:

- лёгкое отступление от «полностью статический HTML» (ADR-0003) — добавляется интерактив
- `Shift+клик` для multi-expand — non-obvious жест; часть пользователей не откроет
- per-project state в localStorage — при смене `project_path` состояние теряется

---

## Architecture Diagram

```
GitHub REST API
       │
       ▼
   github.py
       │
       ▼
     db.py
       │
       ▼
  report.py
       │
       ▼
 report.html
```
