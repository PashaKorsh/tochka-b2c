# tochka-b2b — handoff for AI agents

Передача накопленного опыта по проекту B2B-сервиса NeoMarket. Файл подхватывается
Claude Code автоматически; другим агентам передаётся вручную.

---

## 1. Что строим

FastAPI + SQLAlchemy 2 (async) + PostgreSQL 15. Один процесс, монолит,
модули: `auth`, `categories`, `products`, `invoices`, `inventory`, `moderation`.

User-story квесты идут серией US-B2B-01 … US-B2B-12+:
- 01–04 — лайфцикл товара (create / list / patch / delete) + каскадные события.
- 05 — карточка товара (seller + service mode).
- 06 — накладные.
- 07 — публичный B2C-каталог.
- 08–10 — инвентарь: reserve / unreserve / fulfill.
- 11 — список продавца (IDOR-safe + аггрегаты).
- 12 — удаление SKU с guardrails.

---

## 2. Источники истины (важно: спека > канона)

Два источника:
1. **OpenAPI-спека** в репозитории `URFU2026-NeoMarket/neomarket-protocols`,
   файл `b2b/neomarket-b2b.yaml`. Это **контракт** — пути, схемы запросов /
   ответов, enum-значения, коды статусов.
2. **Канон-flow** в `URFU2026-NeoMarket/neomarket-canon`,
   файл `flows/b2b-flows.md` (секции `#create-product`, `#add-sku`,
   `#edit-product`, `#delete-product`, `#view-product`, `#catalog-for-b2c`,
   `#reserve-sku`, `#fulfill-delivery`, `#apply-moderation`,
   `#list-products`, `#delete-sku`, `#create-invoice`). Это **бизнес-логика** —
   состояния, переходы, побочные эффекты, идемпотентность.

### Правило разрешения конфликтов

**Арбитр (Контракция) последовательно ругал реализацию за выбор канона над спекой.**
Все полученные REJECT-ы — это места, где задание / канон расходились со
спецификацией, и я выбрал канон. После 5 рефейсов рабочее правило:

> **Контракт (пути, шейпы, enum-значения, коды статусов) — строго по спеке.**
> Канон используется для бизнес-логики (порядка проверок, переходов статусов,
> каскадных событий), но любые видимые клиенту артефакты — из `neomarket-b2b.yaml`.

Конкретно по типам конфликтов:

| Что | Канон / задание | Спека | Что брать |
|---|---|---|---|
| HTTP-метод | (часто отличается) | `patch` / `delete` / `post` | **спека** |
| Путь | `/reserve`, `/products` mode-switch | `/api/v1/inventory/reserve`, `/api/v1/public/products` | **спека** |
| Имя поля | `status`, `blocking_reason` (nested) | `event_type`, `blocking_reason_id` (flat) | **спека** |
| Обязательные поля | (опускают `order_id`, `occurred_at`) | required list | **спека** (добавить) |
| Enum-значения | `PENDING` | `[CREATED, PARTIALLY_ACCEPTED, ACCEPTED, CANCELLED]` | **спека** |
| Код успеха | `200 {ok: true}` | `204 No Content` / `InventoryOrderResponse` | **спека** |
| Шейп ответа | вложенные объекты / `{ok}` | `ProductPublicShortResponse`, `InventoryOrderResponse` | **спека** |
| Бизнес-проверки | «всегда clamp до 0» | «явный отказ» | **канон** (явный отказ → 409) |
| Переходы статусов | подробные таблицы | (часто не описаны) | **канон** |

Если задание явно расходится со спекой — задавать вопрос пользователю, но
дефолт — **спека**.

### Где взять спеку локально

```bash
git clone https://github.com/URFU2026-NeoMarket/neomarket-protocols.git \
    ${TEMP:-/tmp}/neomarket-protocols
# Файл: b2b/neomarket-b2b.yaml
```

Канон-flow удобно фетчить через `WebFetch` по
`https://github.com/URFU2026-NeoMarket/neomarket-canon/blob/main/flows/b2b-flows.md`.

---

## 3. На что арбитр смотрит (паттерны REJECT-ов)

Уроки конкретных арбитражей:

- **US-B2B-06.** Использовал `PENDING` (канон) — арбитр: enum `InvoiceStatus`
  в спеке `[CREATED, PARTIALLY_ACCEPTED, ACCEPTED, CANCELLED]`, своих значений
  нельзя. **→ всегда сверять enum со спекой.**
- **US-B2B-07.** Сделал mode-switch `GET /products` (канон) + полный
  `ProductPublicResponse` в списке. Арбитр: путь должен быть
  `/api/v1/public/products`, элементы — `ProductPublicShortResponse`.
  **→ публичный API не отдаёт «полные» карточки в списках; пути не объединять,
  если спека их разделяет.**
- **US-B2B-08.** Опустил `order_id` в `ReserveRequest`, путь `/reserve` без
  `/inventory/`. Арбитр: `required: [idempotency_key, order_id, items]` +
  путь `/api/v1/inventory/reserve`. **→ перепроверять список required и
  каждый сегмент пути.**
- **US-B2B-09.** Сделал поле `status`, вложенный `blocking_reason {id,title,comment}`,
  путь `/events/moderation`, ответ 200 `{ok,applied}`. Арбитр: `event_type`,
  плоский `blocking_reason_id` + отдельный `moderator_comment`, путь
  `/api/v1/moderation/events`, ответ 204 No Content; ещё обязательный
  `occurred_at`. **→ все четыре оси (путь, имя полей, обязательные поля,
  код ответа) проверять отдельно, не «срисовывать» с задания.**
- **US-B2B-10.** Возвращал `{ok: true}` вместо `InventoryOrderResponse
  {order_id, status, processed_at}`. Также `max(0, …)` в fulfill заглушал
  отказы. Арбитр: тело по спеке + явный 409 при `reserved < requested`.
  **→ не «молчаливые» clamp-ы; и тело ответа полностью соответствует схеме
  из спеки, даже если кажется избыточным.**

Универсальный чек-лист перед PR:
- [ ] Путь дословно как в `paths:` спеки (включая префиксы вроде `/public/`, `/inventory/`).
- [ ] Все `required` поля запроса в схеме (и `min_length=1` где `minItems: 1`).
- [ ] Имена полей дословно как в спеке (snake_case, никаких переименований).
- [ ] Enum-значения дословно из `components.schemas.*.enum`.
- [ ] Код ответа: 200/201/204 как в `responses` спеки.
- [ ] Шейп ответа — отдельная схема, не «сборная» из `{ok: true}`.
- [ ] Идемпотентность: повтор не должен повторно вызвать каскад в B2C/Moderation
      (явный тест с `await_count`).

---

## 4. Рабочий процесс

### Ветка и PR

```bash
git fetch origin
git checkout main
git pull --ff-only origin main
git checkout -b feature/us-b2b-XX-short-name   # или fix/us-b2b-XX-...
```

Имя ветки: `feature/us-b2b-XX-<kebab>` для новых US, `fix/us-b2b-XX-<topic>` для
исправлений после REJECT.

`gh` CLI **недоступен** в окружении — PR создаётся через UI:
`https://github.com/PashaKorsh/tochka-b2b/pull/new/<branch>`. В описание PR
обязательно вкладывать ADR-параграф (3–5 предложений) и упоминание `pytest`
(сколько тестов прошло, на чём CI).

### Шаблон коммита

Multi-line `git commit -m "$(cat <<'EOF' ... EOF)"`. Заголовок:
`feat(b2b): US-B2B-XX короткое описание` или
`fix(b2b): US-B2B-XX что починили`. В теле — какие файлы, что меняется по
контракту, что по бизнес-логике, итог по тестам. Co-Authored-By: Claude Opus 4.7
на финальной строке.

### Подъём контейнеров и тесты (важно: build перед, stop после)

Полная последовательность для прогона полного pytest на чистом контейнере:

```powershell
# 1. Билд + старт (postgres + app), вне зависимости от текущего состояния
docker compose up -d --build

# 2. Тестовая БД (одноразово на свежий volume; ошибка «already exists» ок)
docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE tochkab2b_test" 2>&1 | Out-Null

# 3. Прогон pytest в контейнере app, с TEST_DATABASE_URL на тестовую БД
docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/tochkab2b_test `
    app pytest tests/ -q --tb=short

# 4. Остановка контейнеров после тестов
docker compose down
```

Эквивалент на bash:

```bash
docker compose up -d --build \
  && docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE tochkab2b_test" \
       2>/dev/null || true
docker compose exec -T -e TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/tochkab2b_test \
    app pytest tests/ -q --tb=short
docker compose down
```

Если Docker Desktop не запущен — запросить запуск у пользователя
(не пытаться `Start-Process` без подтверждения). После исправлений в коде
re-run только пункта 3 — пересборка обычно не нужна (volume-mount исходников
в docker-compose.yml).

---

## 5. Конвенции кода

- Сервисный слой (`*Service` static methods) — все бизнес-правила, в т.ч.
  IDOR-проверка, ordered guardrails, идемпотентность через таблицу-лог.
- Роуты — тонкие: `try / except ValueError` + маппинг через
  `_edit_error_response` (products) / `_value_error_response` (inventory).
  Возвращают `JSONResponse` для ошибок и pydantic-схему / `Response(204)`
  для успеха.
- Ошибки канона — `{"code", "message"}` (см. `ErrorResponse`).
  Глобальные обработчики в `backend/main.py` приводят 401/403/404/422
  к этому формату.
- Идемпотентность каскадных событий — стабильный `uuid5` per-entity для
  одноразовых (CREATED/DELETED по `product_id`), `uuid4` per-call для
  множественных (EDITED). Лог-таблицы: `inventory_operations`,
  `processed_moderation_events`.
- IDOR — `seller_id` всегда из `current_seller.id` (JWT), `?seller_id=` в
  query игнорируется FastAPI как unknown param.
- Cross-service auth — `X-Service-Key`, валидация в `require_service_key`
  dependency. Default env `SERVICE_API_KEY=dev-service-key` для тестов.
- `get_product_viewer` (products/router.py) — резолвит JWT vs X-Service-Key
  для эндпоинтов с двумя режимами (`GET /products/{id}`).
- Cross-module импорты в сервисах — допустимы; при риске цикла
  (products ↔ inventory) использовать lazy-import внутри метода.

---

## 6. Известные баги/обходы

- На Windows git ругается `LF will be replaced by CRLF` — игнорировать.
- `Edit` tool иногда не находит совпадение из-за невидимых отличий — давать
  больше контекста (соседнюю строку), либо `Write` целиком.
- Не делать ребейз, не закоммитив рабочие правки — git refuses. Перед
  `git rebase origin/main` сначала `git add -A && git commit` или stash.
- При ребейзе фикса на новую `main` ожидать конфликты в `schemas.py` /
  `service.py` / `router.py` — там копится наибольшее число изменений.

---

## 7. Что ещё не сделано

К моменту записи документа:
- Outbox-паттерн для каскадных событий (сейчас fire-and-forget после commit).
- TTL-cleanup `inventory_operations` (сейчас ключи хранятся навсегда).
- CI-workflow в `.github/workflows/tests.yml` уже есть, но `on_event("startup")`
  в FastAPI 0.136 даёт DeprecationWarning — переезд на lifespan-handlers
  отдельной задачей.
- PR-ы в `neomarket-protocols` для расхождений, обнаруженных по ходу:
  обогащение `ProductResponse` полями `blocking_reason {id,title,comment}`,
  `field_reports[]`, `skus_count` / `total_active_quantity` в
  `ProductShortResponse` и т. п. — задокументированы в описаниях прошлых PR.
