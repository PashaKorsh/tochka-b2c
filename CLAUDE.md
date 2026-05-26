# tochka-b2b — handoff for AI agents

Передача накопленного опыта по B2B-сервису NeoMarket. Файл подхватывается
Claude Code автоматически; другим агентам передаётся вручную.

Этот репозиторий — **только B2B-часть** многосервисного проекта NeoMarket
(B2B / B2C / Moderation). Контракты межсервисных вызовов и публичных API
лежат в отдельном репозитории `neomarket-protocols` — там и есть «истина».
Соседние сервисы (B2C, Moderation) разрабатываются другими командами; у них
свои репы со своими реализациями, но **общая** OpenAPI-спека.

---

## 1. TL;DR — что главное для арбитра «Контракции»

Все REJECT-ы по моим сдачам были одного класса: **реализация разошлась со
спекой, обычно потому что я следовал тексту задания или канон-flow**. Урок:
**спека `neomarket-protocols` — главный источник истины для контракта**,
канон — только для бизнес-логики. Краткий чек-лист перед PR (раздел 3 ниже —
с примерами):

- [ ] Путь дословно как в `paths:` спеки, включая префиксы (`/public/`,
      `/inventory/`, `/moderation/`).
- [ ] HTTP-метод как в спеке (`PATCH` vs `PUT` и т. п.).
- [ ] Все `required` поля запроса в Pydantic-схеме (включая `min_length=1`
      там, где в спеке `minItems: 1`).
- [ ] Имена полей дословно как в спеке (snake_case, никаких переименований
      вроде `status → event_type`, никаких вложенных объектов, если в спеке
      плоский `xxx_id`).
- [ ] Enum-значения дословно из `components.schemas.*.enum` (не придумывать
      своих вроде `PENDING`, если в enum его нет).
- [ ] Код ответа: 200 / 201 / 204 строго как в `responses:` спеки.
- [ ] Шейп ответа — отдельная схема из спеки (`ProductPublicShortResponse`,
      `InventoryOrderResponse`, …), не самодельный `{ok: true}`.
- [ ] Идемпотентность: повтор не должен повторно вызвать каскад в B2C /
      Moderation (явный тест с `mock.await_count`).
- [ ] Явные отказы вместо молчаливых `max(0, ...)` / clamp-ов.

---

## 2. Что строим (B2B service)

FastAPI + SQLAlchemy 2 (async) + PostgreSQL 15. Один процесс, монолит,
модули:

```
backend/modules/
├── auth/         # Seller (JWT)
├── categories/   # справочник
├── products/     # Product, SKU, Image, Characteristic, BlockingReason, FieldReport
├── invoices/     # накладные на пополнение остатков
├── inventory/    # reserve / unreserve / fulfill (логи операций)
└── moderation/   # приём событий MODERATED/BLOCKED от Moderation Service
```

User-story квесты идут серией US-B2B-01 … US-B2B-12+:
- 01–04 — лайфцикл товара (create / list / patch / delete) + каскадные события.
- 05 — карточка товара (seller + service mode).
- 06 — накладные.
- 07 — публичный B2C-каталог (`/public/products`, X-Service-Key).
- 08–10 — инвентарь: reserve / unreserve / fulfill.
- 11 — список продавца (IDOR-safe + аггрегаты).
- 12 — удаление SKU с guardrails.

Возможные следующие квесты идут уже на стороне **B2C-сервиса** (отдельный
репозиторий). На стороне B2B они выглядят как опубликованные endpoints,
которые B2C проксирует с `X-Service-Key`.

---

## 3. Источники истины

Два уровня:

1. **OpenAPI-спека — контракт.** Репозиторий
   `URFU2026-NeoMarket/neomarket-protocols`, ветка `main`/`master`. Один
   файл на сервис:
   - B2B → `b2b/openapi.yaml`
   - B2C → `b2c/openapi.yaml` (объединённая спека, секции «каталог»,
     «корзина», «избранное», «заказы», «главная»)
   - Moderation → `moderation/openapi.yaml`
   - Общие схемы → `shared/schemas.yaml`

   ⚠️ В прошлой версии репо файлы назывались
   `<service>/neomarket-<service>.yaml`; недавно переименованы в
   `<service>/openapi.yaml`. В моих описаниях прошлых PR и в коде ещё
   встречаются старые имена — это исторические ссылки, актуальный путь
   `<service>/openapi.yaml`.

   Локально проще всего склонировать целиком:

   ```bash
   git clone https://github.com/URFU2026-NeoMarket/neomarket-protocols.git \
       ${TEMP:-/tmp}/neomarket-protocols
   ```

   В задании могут приходить ссылки на спеку в **canon-репозитории**
   (`neomarket-canon/apis/<service>/openapi.yaml`) — это **черновики** или
   старые версии. **Authoritative — всегда `neomarket-protocols`.**

2. **Канон-flow — бизнес-логика.** Репозиторий
   `URFU2026-NeoMarket/neomarket-canon`, `flows/<service>-flows.md`
   (например, `flows/b2b-flows.md`, `flows/b2c-catalog-flows.md`). Здесь —
   состояния, переходы, побочные эффекты, идемпотентность, формулы видимости.

### Правило разрешения конфликтов

**Арбитр (Контракция) последовательно ругал реализацию за выбор канона над
спекой.** После пяти REJECT-ов рабочее правило:

> **Контракт** (пути, HTTP-методы, шейпы, enum-значения, коды статусов,
> заголовки авторизации) — **строго по спеке `neomarket-protocols`**.
> **Канон** — для бизнес-логики (порядок проверок, переходы статусов,
> каскадные события), но любые видимые клиенту артефакты — из openapi.yaml.

| Что | Канон / задание | Спека | Что брать |
|---|---|---|---|
| HTTP-метод | (часто отличается) | `patch` / `delete` / `post` | **спека** |
| Путь | `/reserve`, `/products` mode-switch, `/events/moderation` | `/api/v1/inventory/reserve`, `/api/v1/public/products`, `/api/v1/moderation/events` | **спека** |
| Имя поля | `status`, `blocking_reason` (nested) | `event_type`, `blocking_reason_id` (flat) + `moderator_comment` | **спека** |
| Обязательные поля | (опускают `order_id`, `occurred_at`) | required list | **спека** (добавить) |
| Enum-значения | `PENDING` | `[CREATED, PARTIALLY_ACCEPTED, ACCEPTED, CANCELLED]` | **спека** |
| Код успеха | `200 {ok: true}` | `204 No Content` / `InventoryOrderResponse {order_id, status, processed_at}` | **спека** |
| Шейп ответа | вложенные объекты / `{ok}` | `ProductPublicShortResponse`, `InventoryOrderResponse` | **спека** |
| Бизнес-проверки | «всегда clamp до 0» | (часто не описаны явно) | **канон** (явный отказ → 409) |
| Переходы статусов | подробные таблицы | (часто не описаны) | **канон** |

Если задание явно расходится со спекой — задавать вопрос пользователю
(`AskUserQuestion`), но дефолт-рекомендация — **спека**.

---

## 4. На что арбитр смотрит (паттерны REJECT-ов)

Уроки конкретных арбитражей — короткий разбор каждого, чтобы видеть оси
проверки контракта:

- **US-B2B-06 (накладные).** `PENDING` (канон) → арбитр: enum `InvoiceStatus`
  в спеке `[CREATED, PARTIALLY_ACCEPTED, ACCEPTED, CANCELLED]`, своих значений
  нельзя. **→ enum-значения ВСЕГДА сверять со спекой.**
- **US-B2B-07 (B2C-каталог).** Mode-switch `GET /products` (канон) + полный
  `ProductPublicResponse` в списке. Арбитр: путь должен быть
  `/api/v1/public/products`, элементы — `ProductPublicShortResponse`.
  **→ публичный API не отдаёт «полные» карточки в списках; пути не объединять,
  если спека их разделяет.**
- **US-B2B-08 (reserve).** Опустил `order_id` в `ReserveRequest`, путь
  `/reserve` без `/inventory/`. Арбитр: `required: [idempotency_key, order_id,
  items]` + путь `/api/v1/inventory/reserve`. **→ перепроверять список
  required и каждый сегмент пути.**
- **US-B2B-09 (moderation).** Поле `status`, вложенный
  `blocking_reason {id,title,comment}`, путь `/events/moderation`, ответ 200
  `{ok,applied}`. Арбитр: `event_type`, плоский `blocking_reason_id` +
  отдельный `moderator_comment`, путь `/api/v1/moderation/events`, ответ
  204 No Content; ещё обязательный `occurred_at`. **→ все четыре оси (путь,
  имена полей, обязательные поля, код ответа) проверять отдельно, не
  «срисовывать» с задания.**
- **US-B2B-10 (fulfill).** Возвращал `{ok: true}` вместо
  `InventoryOrderResponse {order_id, status, processed_at}`. Также
  `max(0, …)` в fulfill заглушал отказы. Арбитр: тело по спеке + явный 409
  при `reserved < requested`. **→ не «молчаливые» clamp-ы; тело ответа
  полностью соответствует схеме из спеки, даже если кажется избыточным.**

Общий паттерн: **арбитр сличает каждое поле / каждый сегмент пути / каждый
код ответа с openapi.yaml**. Любое отклонение — REJECT, независимо от
качества бизнес-логики.

---

## 5. Особенности B2C-/межсервисных задач

Если квест — на стороне B2C или другого сервиса (либо B2B-эндпоинт,
который консьюмит B2C), особенности следующие:

- **Где спека.** `neomarket-protocols/b2c/openapi.yaml` (одним файлом
  агрегирует «каталог», «корзину», «избранное», «заказы», «главную»).
  В задании могут давать ссылку на черновик в `neomarket-canon/apis/b2c/...`
  — это не authoritative; финальная истина в protocols. Если контракт ещё
  не опубликован в protocols — внести PR туда (бонус-возможность).

- **Авторизация к B2B.** B2C ходит в B2B по `X-Service-Key`. У B2B
  публичные межсервисные endpoints — `/api/v1/public/products`,
  `/api/v1/inventory/*`. Реализованы в этом репозитории, реальные
  значения ключа задаются env-переменными (`SERVICE_API_KEY`,
  `B2B_TO_B2C_KEY`, `B2C_TO_B2B_KEY` и т. п.).

- **Видимость каталога.** B2B уже фильтрует
  `status=MODERATED AND deleted=false AND active_quantity>0` —
  см. `ProductService.list_catalog_products` и
  `_to_public_short_response`. B2C **только проксирует** запрос с нужными
  фильтрами / `?ids=`, не дублирует бизнес-фильтрацию.

- **Фасеты и сортировки.** Если задание просит facets — обычно отдельный
  endpoint (например, `GET /catalog/facets`), считающий
  `COUNT(*) GROUP BY <attr>` по тому же visibility-предикату. На стороне
  B2B пока такого endpoint нет; добавлять — отдельным квестом и сначала
  PR в `neomarket-protocols`.

- **Невалидный `sort`** → 400 `INVALID_REQUEST` с **перечислением
  допустимых значений** в `message`. Пример: `"sort must be one of:
  price_asc, price_desc, date_desc"`. Не 422 — DoD обычно требует именно
  400 (как в b2b-flows для quantity / name).

- **Недоступность апстрима (B2B → B2C → 502).** Сетевые ошибки httpx
  ловить и возвращать `502 Bad Gateway` (либо `503 Service Unavailable`)
  с телом `{"code": "UPSTREAM_UNAVAILABLE", "message": "..."}`. В B2B
  сейчас все каскадные HTTP-вызовы — fire-and-forget `try/except` без
  возврата 502 наверх (события не блокируют ответ продавцу). Для **прокси**
  это **не подходит** — для прокси нужно явно поднять 502.

- **Идемпотентность по `idempotency_key`** должна быть и на стороне B2C
  (если она перенаправляет write-операции), и на стороне B2B (уже есть —
  `inventory_operations`, `processed_moderation_events`).

---

## 6. Контракция-проверка перед PR (чек-лист длинной версии)

Прогнать руками по каждому новому/изменённому endpoint:

1. **Путь.** Открыть нужный `.yaml` в `neomarket-protocols`, найти секцию
   `paths:` — путь должен совпасть посимвольно, включая префикс версии
   (`/api/v1/...`) и сегменты типа `/public/`, `/inventory/`,
   `/moderation/`. **Не объединять два пути в один с mode-switch по
   заголовку**, если спека их разделяет.

2. **HTTP-метод.** Тоже из секции `paths:`. `PATCH` / `PUT` / `DELETE` —
   спека права.

3. **Авторизация.** В спеке у operation: `security:` (Bearer JWT) или
   `parameters: X-Service-Key`. Реализовать ровно как там; без ключа /
   токена → 401 `{"code":"UNAUTHORIZED","message":"..."}`.

4. **Request body.**
   - `required: [...]` ⇒ Pydantic-поле без `default`, либо
     `Field(..., min_length=1)` для не-пустых строк / списков.
   - Имена полей дословно (snake_case, никаких переименований).
   - Вложенные объекты только там, где они вложенные в спеке. Если в спеке
     плоский `xxx_id`, не делайте `nested_obj.id`.
   - `nullable: true` ⇒ `Optional[X] = None`.
   - `format: uuid` ⇒ `UUID` (Pydantic валидирует автоматически).

5. **Response body.**
   - Использовать **именованную схему** из `components.schemas`
     (`ProductPublicShortResponse`, `InventoryOrderResponse`, …),
     а не самодельный `{ok: true}`.
   - Все `required:` поля присутствуют в Pydantic-модели.
   - `enum:` поля — отдельный Python-`Enum`, значения дословно.

6. **Status codes.** В `responses:` обычно явно прописаны 200 / 201 / 204 /
   400 / 401 / 403 / 404 / 409 / 422. Брать ровно те, что заявлены.
   `204` ⇒ FastAPI `Response(status_code=204)`, **тело пустое**.

7. **Error shape.** Один формат: `{"code", "message", "details?"}`.
   Глобальные exception-handler'ы в `backend/main.py` уже приводят
   `HTTPException(detail={code,message})` и `RequestValidationError`
   к этому виду.

8. **Идемпотентность.** Повтор одного и того же ключа:
   - возвращает прежний 200/204 без побочных эффектов;
   - **не повторяет каскад в B2C / Moderation** (тест
     `mock.await_count == 1` после двух одинаковых запросов).

9. **Канон-проверки.** Порядок guardrail'ов критичен (например, для
   DELETE SKU — `HARD_BLOCKED → reserves → delete`). Записать в
   docstring сервиса со ссылкой на канон. **Явные отказы** (raise →
   400/409) вместо `max(0, …)`.

10. **Тесты.** Каждый named-сценарий DoD = отдельная `@pytest.mark.asyncio`
    функция с тем же именем (например, `test_catalog_returns_filtered_sorted_products`).
    Тесты идут на **реальной PostgreSQL** в docker-compose, не на моках.

---

## 7. Рабочий процесс

### Ветка и PR

```bash
git fetch origin
git checkout main
git pull --ff-only origin main
git checkout -b feature/us-XX-YY-short-name   # или fix/us-XX-YY-...
```

Имя ветки:
- `feature/us-b2b-XX-<kebab>` / `feature/us-b2c-XX-<kebab>` / `feature/us-mod-XX-<kebab>` для новых US.
- `fix/us-XX-YY-<topic>` для исправлений после REJECT.

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

### Подъём контейнеров и тесты — **build перед, stop после**

Полная последовательность для прогона полного pytest на чистом контейнере.
**Обязательно** делать `up --build` перед прогоном (включая первый запуск
после `git pull`, где зависимости могут отличаться) и `down` после — это
часть рабочей инструкции.

```powershell
# 1. Билд + старт (postgres + app), даже если контейнеры были подняты —
#    --build пересобирает образ под новые requirements/код.
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
(не пытаться `Start-Process` без подтверждения).

При итеративной правке кода (без правок в `requirements.txt` /
`Dockerfile`) повторно гонять можно без `--build` — `volumes: - .:/app`
в docker-compose.yml монтирует исходники в контейнер.

---

## 8. Конвенции кода

- Сервисный слой (`*Service` static methods) — все бизнес-правила, в т.ч.
  IDOR-проверка, ordered guardrails, идемпотентность через таблицу-лог.
- Роуты — тонкие: `try / except ValueError` + маппинг через
  `_edit_error_response` (products) / `_value_error_response` (inventory).
  Возвращают `JSONResponse` для ошибок и pydantic-схему / `Response(204)`
  для успеха.
- Ошибки канона — `{"code", "message"}` (см. `ErrorResponse`). Глобальные
  обработчики в `backend/main.py` приводят 401/403/404/422 к этому формату.
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

## 9. Известные баги/обходы

- На Windows git ругается `LF will be replaced by CRLF` — игнорировать.
- `Edit`-инструмент иногда не находит совпадение из-за невидимых отличий
  — давать больше контекста (соседнюю строку), либо `Write` целиком.
- Не делать ребейз, не закоммитив рабочие правки — git refuses. Перед
  `git rebase origin/main` сначала `git add -A && git commit` или stash.
- При ребейзе фикса на новую `main` ожидать конфликты в `schemas.py` /
  `service.py` / `router.py` — там копится наибольшее число изменений.

---

## 10. Что ещё не сделано

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