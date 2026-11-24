# ЗАДАЧА ДЛЯ CLINE
Прочти сначала файлы: app/db.py, app/calc_run.py, app/excel_io.py, app/web/routes.py, app/web/templates/*.html, tests/test_web_pages.py.
Бэкенд УЖЕ ГОТОВ — НЕ МЕНЯТЬ: app/db.py, app/calc.py, app/calc_run.py, app/excel_io.py, app/onec.py, app/price_source.py, updater.py.
Меняются только app/web/routes.py, app/web/templates/*, tests/test_web_pages.py.

# ТЗ: редизайн веб-интерфейса «Себестоимость МП» (Пульт v3)

Flask + SQLite + Jinja2, Python 3.10+, без новых зависимостей, тексты и комментарии на русском.
Приложение: модуль `sebestoimost_mp`, Blueprint `web_bp` в `app/web/routes.py`, шаблоны в `app/web/templates/`.

## Что УЖЕ сделано в бэкенде (НЕ менять эти файлы: db.py, calc.py, calc_run.py, excel_io.py, onec.py, price_source.py, updater.py)

**products** (новые колонки): `is_active` (1=поставляется), `margin_pct`, `ppu_rub_per_kg`, `extras_rub` (NULL = глобальный дефолт из settings), `article_wb`, `article_ozon` (артикулы МП для мэтчинга).

**template_profiles**: id, marketplace, name, sheet, header_row, data_row, col_article_mp, col_article_supplier (NULL), col_cost_write, updated_at. Сиды:
- `wb`/`wb`: sheet `Себестоимость за {YYYY-MM}`, header 2, data 3, A, B, E;
- `ozon`/`prices`: sheet `Товары и цены`, header 2, data 5, A, NULL, BD;
- `ozon`/`finance`: sheet `Sheet1`, header 1, data 2, A, NULL, B.

**API (уже есть, использовать):**
- `calc_run.run_calculation(con, calc_date, marketplaces: dict[str, list[dict]], prices, price_errors) -> run_id`. Ключи marketplaces — КЛЮЧИ ПРОФИЛЕЙ (`wb`, `ozon_prices`, `ozon_finance`). items: `{'row', 'article_mp', 'article_supplier'}` (у Ozon supplier=None — мэтчинг по article_ozon уже внутри). is_active=0 пропускаются.
- `calc_run.get_last_run(con)`, `get_run_costs(con, run_id, marketplace_key) -> {article_supplier: cost}`, `get_run_problems(con, run_id)`, `get_price_history(con, product_id) -> [(calc_date, price, cost, marketplace)]`, `get_last_costs_map(con) -> {product_id: (cost, prev_cost, jump)}`, `get_no_price_count(con, run_id=None) -> int`.
- `excel_io.Profile` (dataclass, поля как template_profiles) + `Profile.from_row(row)`; `read_items_profile(path, profile) -> items`; `write_costs_profile(src, dst, profile, costs: {row_num: cost}, calc_date=None) -> int` (при `{YYYY-MM}` в sheet — переименовывает лист по месяцу calc_date); `detect_profile(path) -> {sheet, header_row, data_row, col_article_mp, col_article_supplier, col_cost_write, warnings}`.
- `price_source.load_params(root) -> tuple|None`, `price_source.fetch_prices(products, pm0, pm1) -> (prices, errors)`.
- В routes.py уже есть хелперы: get_db(), allowed_file, _row_dict, _today, _parse_date_or_today, _parse_decimal, _parse_non_negative_decimal, _parse_int, _last_upload(db, marketplace), _last_setting_value, _redirect_back. СОХРАНИТЬ.

## Ключи профилей и uploads

Везде ключ профиля: `wb`, `ozon_prices`, `ozon_finance`. `uploads.marketplace` хранит ключ профиля. Профили грузить: `SELECT * FROM template_profiles` → `excel_io.Profile.from_row`.

## Страницы (светлая тема, см. стиль ниже)

### 1. `/` — Пульт (главная, заменяет index)
- Пинг 1С при старте приложения и по кнопке «Обновить» (POST `/refresh`): `price_source.load_params(current_app.config['ROOT'])`; ок → кэш в `current_app.config['ONEC_STATUS'] = {'ok': bool, 'at': 'YYYY-MM-DD HH:MM'}`; без config.ini → ok=False, 'не настроена'.
- Плитки-карточки (крупные цифры): 1С УТ 11.5 (Подключено/Нет связи + дата пинга, зелёный/красный), АССОРТИМЕНТ (число products, подпись WB: n • Ozon: n — по мэтчингу артикулов эталонов последних загрузок), ЭТАЛОН WB (позиций + дата загрузки), ЭТАЛОН OZON (позиций prices + finance), БЕЗ ЦЕНЫ 1С (`get_no_price_count`, красная, ссылка на /problems). Плитки Ассортимент/WB/Ozon — ссылки на /assortment и /mp/wb, /mp/ozon.
- Блок «Расчёт себестоимости»: дата (по умолчанию сегодня), кнопка «Рассчитать» (POST /calc), кнопка «Рассчитать и скачать все» (POST /calc с redirect на /download_all), строка последнего прогона (дата, всего/ок/проблем).
- Блок «Файлы для маркетплейсов»: кнопки «Скачать WB» (/download/wb), «Скачать Ozon цены» (/download/ozon_prices), «Скачать Ozon финанс» (/download/ozon_finance).

### 2. `/calc` (POST) — как сейчас, но по профилям
Для каждого из 3 ключей: `_last_upload(db, key)` → `read_items_profile(path, profile)` → marketplaces[key]. Далее как текущий calc (цены 1С для manual_price=0, run_calculation, flash с номером прогона). Поле формы `then=download` → после прогона redirect на /download_all.

### 3. `/download/<key>` и `/download_all`
По ключу: items из последнего upload, `get_run_costs(db, run_id, key)` → costs {row: cost} (мэтчинг: article_supplier если есть, иначе по article_mp через get_run_costs — внимание: get_run_costs отдаёт по article_supplier; для Ozon-профилей брать из run_items по marketplace=key и article_mp — сделать локальный запрос `SELECT article_mp, cost FROM run_items WHERE run_id=? AND marketplace=? AND status='ok'` и мэтчить по article_mp). `write_costs_profile(src, dst, profile, costs, calc_date=run['calc_date'])`. Имя файла: `{key}_sebestoimost_{calc_date}.xlsx`. /download_all — отдать zip с тремя файлами (zipfile, в памяти).

### 4. `/assortment` — общий ассортимент
Таблица всех products: чекбокс, Артикул, Наименование, WB✓/Ozon✓ (по article_wb/article_ozon или попаданию в последние эталоны), Закупка (из last_costs_map/1С), Вес г, ППУ, Наценка %, Допы ₽ (три последних — input'ы overrides, placeholder = глобальный дефолт, пусто = NULL), ИТОГО (cost последнего прогона; жёлтая подсветка строки при jump=1), Поставляется (переключатель). Фильтры GET: q (поиск, Python-lower), mp (wb/ozon), no_price (из последнего прогона problem), inactive. Рядом старая себестоимость (prev_cost). Ссылка «история» → /products/<id>/history.
- POST `/products/<pid>/override`: поля margin_pct/ppu_rub_per_kg/extras_rub, пустая строка → NULL; UPDATE products.
- POST `/products/<pid>/toggle_active`: is_active = 1 - is_active.
- POST `/assortment/bulk`: ids[] + action (activate/deactivate/set_margin/set_ppu/set_extras) + value — массовый UPDATE.

### 5. `/mp/<wb|ozon>` — позиции эталонов МП
Позиции из последних эталонов этого МП (для ozon — объединение prices и finance по article_mp): артикул МП, артикул поставщика, наименование (products.name), закупка, себестоимость из последнего прогона, prev_cost, jump-подсветка, те же inline-действия (override, toggle_active), фильтр «без цены».

### 6. `/problems` — «Без цены 1С» (переименовать заголовок)
Как сейчас + форма ручной цены прямо в строке (POST /products/<pid>/price уже есть). Убрать из навигации слово «Проблемы».

### 7. `/templates` — эталоны
Три блока (wb, ozon_prices, ozon_finance): текущий файл (uploads: filename, uploaded_at, позиций), текущий профиль (лист/строки/колонки), форма загрузки .xlsx (POST /templates/upload, поле profile_key). При загрузке: сохранить файл (как текущий upload: timestamp, secure_filename, принудительно .xlsx, UPLOAD_DIR), `detect_profile(path)` → UPDATE template_profiles (sheet, header_row, data_row, col_article_mp, col_article_supplier, col_cost_write, updated_at) + INSERT uploads + мэтчинг: если у items есть article_supplier — `INSERT OR IGNORE INTO products` + `UPDATE products SET article_wb=?/article_ozon=? WHERE article_supplier=?`; flash с результатом детекта (что нашёл + warnings).

### 8. `/products/<pid>/history` — история цен товара
Таблица из get_price_history: дата прогона, закупка, себестоимость, МП. Без графиков.

### 9. УДАЛИТЬ
Роут `/history` и шаблон history.html. Из base.html убрать ссылку «История». Навигация base.html: Пульт `/`, Ассортимент `/assortment`, Без цены 1С `/problems` (badge с числом), Шаблоны `/templates`, Расходы `/expenses`. Старые роуты products/price/toggle_manual/weight/expenses* СОХРАНИТЬ как есть.

## Стиль (CSS в base.html, светлая тема)
Фон #eef2f5, карточки белые с радиусом 12px и тонкой рамкой #dde4ea, акцент teal #17869b, тёмный teal #14555f (кнопки/заголовки), красный #d64545 (алерты, плитка Без цены), зелёный #2e9e5b (статус 1С ок), жёлтый фон #fff3cd для строк jump=1. Крупные цифры плиток ~30px bold. Таблицы: белые, hover-строки, compact. Без JS-фреймворков; чекбоксы массовых действий — обычная форма; «выделить все» — маленький inline <script>.

## Тесты
Обновить tests/test_web_pages.py: убрать проверки /history, добавить smoke-GET: /, /assortment, /mp/wb, /mp/ozon, /problems, /templates, /expenses, /products/1/history (на тестовой БД с фикстурой — посмотри conftest/текущие тесты и сделай по аналогии). Остальные 61 тест не трогать и не ломать.

## Формат ответа
Полные файлы: `app/web/routes.py`, `app/web/templates/base.html`, `index.html`, `assortment.html`, `mp.html`, `problems.html`, `templates.html`, `product_history.html`, `expenses.html` (без изменений, если не нужно), `tests/test_web_pages.py`. Каждый файл — отдельным блоком ``` с первой строкой-комментарием `# путь` или `<!-- путь -->`.
