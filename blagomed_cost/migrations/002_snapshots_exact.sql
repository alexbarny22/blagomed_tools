-- 002_snapshots_exact.sql — точное хранение Decimal и ключ идемпотентности.
--
-- Почему нужна миграция (этап 6, раздел «Важное ограничение текущей схемы»):
--   в 001_init.sql денежные поля cost_snapshots имеют тип SQLite REAL, который
--   НЕ гарантирует восстановления исходного Decimal (например, 0.1 → float).
--   Согласованное решение: ROW-значения остаются REAL (совместимость с уже
--   импортированными legacy-снимками и отчётами), а точная десятичная запись
--   сохраняется в отдельных текстовых колонках *_exact (str(Decimal)). Legacy-
--   снимки не изменяются (в *_exact у них NULL — чтение использует REAL).
--
-- ВНИМАНИЕ (согласование): к уже существующей рабочей БД миграцию применяет
-- db.apply_migrations автоматически (backup перед изменением схемы делается
-- сам). Это безопасно и совместимо, но перед обновлением «боевой» копии с
-- историей требуется подтверждение владельца (правило проекта).
--
-- Одновременно добавляется runs.run_key — подпись запуска (SHA-1 входа),
-- по которой различаются: повторная техническая попытка завершить один и тот
-- же run (status='running', continue) и новый самостоятельный расчёт (новый
-- ключ). UNIQUE-индекс разрешает NULL (legacy-запуски остаются без ключа).

ALTER TABLE cost_snapshots ADD COLUMN price_1c_exact  TEXT;  -- str(Decimal), точная закупочная цена
ALTER TABLE cost_snapshots ADD COLUMN ppu_rate_exact   TEXT;  -- str(Decimal), ставка ППУ, руб/кг
ALTER TABLE cost_snapshots ADD COLUMN ppu_rub_exact    TEXT;  -- str(Decimal), стоимость ППУ, руб
ALTER TABLE cost_snapshots ADD COLUMN extras_rub_exact TEXT;  -- str(Decimal), сумма доп. расходов, руб
ALTER TABLE cost_snapshots ADD COLUMN margin_pct_exact TEXT;  -- str(Decimal), наценка, %

ALTER TABLE runs ADD COLUMN run_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_run_key ON runs (run_key);

PRAGMA user_version = 2;