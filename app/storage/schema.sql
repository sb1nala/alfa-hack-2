-- Схема базы бота. Применяется при каждом старте, поэтому все команды должны быть повторяемыми:
-- CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS, ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
-- Время храним в UTC, тип timestamptz.
-- Таблицы (пользователи, профили, напоминания, события, история показов) добавляет роль 2.

SELECT 1;
