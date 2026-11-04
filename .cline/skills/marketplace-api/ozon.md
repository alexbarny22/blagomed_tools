# Ozon Seller API — выжимка

Сверено: 2026-11-05. Источники:
- [S1] официальный канал уведомлений Ozon Seller API (t.me/OzonSellerAPI) и новости dev.ozon.ru;
- [S2] живые ответы API от 02.11.2026 из открытого проекта evelagin/evetis-wb-analytics (`pipelines/ozon/runtime/promo.py`, ветка release/evetis-ozon-runtime-08dfb88-promo);
- [S3] Go-клиент diphantxm/ozon-api-client (коммит 2025-10-20) — структуры запросов/ответов;
- [S4] живая работа нашего репрайсера `ozon_repricer/core/ozon.py`.

## Общее

- Адрес: `https://api-seller.ozon.ru`. Все методы `POST` с JSON, кроме `GET /v1/actions`.
- Заголовки: `Client-Id`, `Api-Key`, `Content-Type: application/json`. Ключи — только из локальных настроек, никогда в репозиторий.
- 429 и 5xx — повторять с паузой (в репрайсере: 5 попыток, пауза растёт). 401/403 — неверный ключ или у ключа нет прав.
- Цены в запросах на запись — **строками** (`"1290"`), в ответах — числами.
- Лимит изменения цены: около 10 раз в час на товар [S3-описание]. Репрайсер сам ограничивает 8 в сутки.

## Цены и товары

### `/v5/product/info/prices` — цены, комиссии, индексы, акции товара  (Сверено: S3, S4, S2 02.11.2026)
Запрос: `{"cursor": "", "limit": N, "filter": {"offer_id": [], "product_id": [], "visibility": "ALL"}}`.
Пагинация: `cursor` из ответа, пока не пустой. В репрайсере `limit` 1000 — работает.
Ответ: `{"items": [...], "cursor": "...", "total": N}`. Элемент `items[]`:
- `product_id`, `offer_id`, `acquiring` (эквайринг, ₽), `volume_weight`
- `price`: `price` (ваша цена селлера), `old_price` (зачёркнутая), `marketing_seller_price` (цена с акциями продавца — точная),
  `min_price` (мин. цена в Ozon), `min_ozon_price`, `retail_price`, `vat`, `currency_code`, `auto_action_enabled`,
  `auto_add_to_ozon_actions_list_enabled` (с 04.2025), `net_price`, `declared_price` (с 28.09.2026).
  **`marketing_price` удалён с 12.11.2025 — цену покупателя API не отдаёт.**
- `commissions`: `sales_percent_fbo`, `sales_percent_fbs` (% комиссии), `fbo_deliv_to_customer_amount`, `fbs_deliv_to_customer_amount` (последняя миля),
  `fbo_direct_flow_trans_min_amount` / `_max_amount`, `fbs_direct_flow_trans_min_amount` / `_max_amount` (логистика), `fbo_fulfillment_amount`,
  `fbs_first_mile_min_amount` / `_max_amount`, `fbo_return_flow_amount`, `fbs_return_flow_amount`.
- `marketing_actions`: `actions[]` (`title`, `date_from`, `date_to`, `value`), `current_period_from/to`, `ozon_actions_exist`
  (⚠ флаг ненадёжен: наблюдался `false` при непустом `actions[]` [S2]).
- `price_indexes`: `color_index` (`GREEN`/`YELLOW`/`RED`…), `ozon_index_data`, `external_index_data`, `self_marketplaces_index_data` —
  в каждом `min_price`, `min_price_currency`, `price_index_value`. Расчёт ≈ цены покупателя — функция `buyer_estimate()` в репрайсере.

### `/v1/product/import/prices` — запись цен  (Сверено: S3, S4)
Запрос: `{"prices": [ {...}, ... ]}`, до 1000 товаров за раз. Поля элемента:
`product_id` (число) и/или `offer_id`, `price`, `old_price` (`"0"` — убрать зачёркнутую), `min_price`,
`min_price_for_auto_actions_enabled` (true — Ozon учитывает min_price при автодобавлении в акции), `currency_code` (`"RUB"`),
`auto_action_enabled` (`ENABLED`/`DISABLED`/`UNKNOWN`), `auto_add_to_ozon_actions_list_enabled` (с 04.2025), `price_strategy_enabled`,
`net_price`, `vat`, `declared_price` (с 28.09.2026).
Ответ: `{"result": [ {"product_id", "offer_id", "updated": bool, "errors": [{"code", "message"}]} ]}`.
Правило Ozon по зачёркнутой цене: разница с `price` должна быть не меньше ~5% (до 10 000 ₽) / 500 ₽ (выше) — функция `old_price_for()` в репрайсере.

### `/v3/product/info/list` — названия, SKU, фото  (Сверено: S4)
Запрос: `{"product_id": ["..."]}` (или `offer_id`, `sku`), до 1000.
Ответ: `{"items": [...]}`: `id`, `offer_id`, `sku` (или в `sources[].sku`), `name`, `primary_image` / `images` (строка или список),
`prices`, `price_indexes`, `statuses`, `declared_price` (с 28.09.2026). Разбор фото — `main_image()` в репрайсере.

## Остатки

### `/v4/product/info/stocks`  (Сверено: S3, S4)
Запрос: `{"cursor": "", "limit": 1000, "filter": {"visibility": "ALL"}}`. Пагинация `cursor`.
Ответ: `{"items": [...], "cursor", "total"}`. Элемент: `product_id`, `offer_id`, `stocks[]`:
`type` (`fbo`, `fbs`, `rfbs`, `fbp`), `present` (всего, включая резерв), `reserved`, `sku`, `shipment_type`.
Доступно к продаже = `present − reserved`.

## Акции  — ⚠ С 19.10.2026 ДЕЙСТВУЕТ НОВАЯ ЛОГИКА

**С 19.10.2026 Ozon отключил старые методы товаров акций v1 и ввёл «единую предельную цену» для цен и акций** [S1, S2].
`/v1/actions/products`, `/v1/actions/candidates`, `/v1/actions/auto-add/*` отвечали `400 code 9` («unified price logic») с 14.10 и `404` с 26.10 [S2].
Не используй методы v1 для товаров акций.

### Чтение (Сверено живыми ответами 02.11.2026: S2)
| Метод | Запрос | Ответ |
|---|---|---|
| `GET /v1/actions` — список акций (остался v1) | без тела | `result[]`: `id`, `title`, `action_type`, `description`, `date_start`, `date_end`, `freeze_date` (после неё нельзя повышать цены и убирать товары), `auto_add_dates[]`, `is_participating`, `potential_products_count`, `participating_products_count`, `banned_products_count`, `is_voucher_action`, `with_targeting`, `order_amount`, `discount_type`, `discount_value` |
| `POST /v2/actions/products` — товары, которые участвуют | `{"action_id", "limit" (1..100), "last_id": ""}` | `result.products[]`, `result.total`, `result.last_id` |
| `POST /v2/actions/candidates` — товары, которые могут участвовать | то же | то же |
| `POST /v2/actions/auto-add/products/list` — товары, которые Ozon **добавит** автоматически | `{"action_id", "limit", "offset"}` — здесь **offset** | `result.products[]`, `result.total`; id товара — поле `product_id` |
| `POST /v2/actions/auto-add/products/candidates` | `{"action_id", "limit", "offset"}` | id товара — поле `id` |

Пагинация `products`/`candidates`: первая страница `last_id: ""`, дальше `last_id` из ответа; стоп, если страница короче `limit`,
`last_id` пустой или повторился. `limit` максимум 100. Сверяй собранное с `total`.
Поля товара в `products[]` (у `products`/`candidates` id товара — поле **`id`**): `price`, `action_price`, `max_action_price`,
`alert_max_action_price`, `alert_max_action_price_failed`, `add_mode`, `stock`, `min_stock`, `current_boost`, `price_min_elastic`,
`price_max_elastic`, `min_boost`, `max_boost`; новые в v2: `recommended_stock`, `marketplace_seller_price`, `website_prices`,
`min_seller_price`, `is_quarantined`.
Поля auto-add: `product_id`/`id`, `offer_id`, `sku`, `name`, `price`, `base_price`, `max_discount_price`, `min_seller_price`,
`marketplace_seller_price`, `action_price_to_auto_add`, `min_action_quantity`, `quantity_to_auto_add`, `currency`, `add_mode`,
`has_expired_min_seller_price`, `website_prices`, `will_be_quarantined`, `is_manually_added`.
Ozon добавляет новые поля — незнакомое поле не должно ронять код.

**Кандидат ≠ автодобавление:** `candidates` — «может участвовать», `auto-add/products/list` — «Ozon добавит, и по какой цене» [S2].

### Запись (вход/выход товара в акцию) — ⚠ НЕ ПОДТВЕРЖДЕНО
- Старые `/v1/actions/products/activate` (`{"action_id", "products": [{"product_id", "action_price", "stock"}]}`) и
  `/v1/actions/products/deactivate` (`{"action_id", "product_ids": []}`), ответ `result.product_ids[]`, `result.rejected[]{product_id, reason}` —
  описаны в библиотеках, но **после 19.10.2026 могут быть отключены** вместе с остальными методами v1.
- Новые пути для входа/выхода в v2 в проверенных источниках **не найдены**.
- Есть также методы `/v1/actions/auto-add/products/update` и `/delete` (управление автодобавлением) — статус после 19.10.2026 не подтверждён.
- **Перед реализацией записи:** узнать актуальные методы у пользователя (он может открыть docs.ozon.ru → раздел «Акции»)
  или одной точечной проверкой страницы метода. Затем обновить этот раздел.

### Акции селлера (собственные акции продавца, с 17.02.2026) — для справки
`/v1/seller-actions/list`, `/create/*`, `/update/*`, `/products/add`, `/products/delete`, `/products/candidates`, `/products/list`,
`/change-activity`, `/archive`, `/voucher/get` [S1, dev.ozon.ru «Управление акциями: полный гид»]. Поля не сверены — нашим ботам пока не нужны.

## Изменения площадки (журнал)
| Дата | Что | Источник |
|---|---|---|
| 05.05.2025 | `offset` удалён у `/v1/actions/candidates` и `/v1/actions/products`, вместо него `last_id` | dev.ozon.ru news 510 |
| 04.2025 | Поле `auto_add_to_ozon_actions_list_enabled` в `/v1/product/import/prices` и `/v5/product/info/prices` | S1 |
| 03.2025 | Методы `/v1/actions/hotsales/*` удалены | S1 |
| 12.11.2025 | `marketing_price` удалён из `/v5/product/info/prices` и `/v3/product/info/list` | cleverence.ru |
| 28.09.2026 | Поле `declared_price` в `/v5/product/info/prices`, `/v3/product/info/list`, `/v1/product/import/prices` | S1 |
| 19.10.2026 | Отключены методы товаров акций v1, новая логика акций и «единая предельная цена» (постепенный ввод) | S1, S2 |
