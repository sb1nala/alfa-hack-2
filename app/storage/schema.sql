-- Applied at every startup. IF NOT EXISTS preserves existing rows; it does not
-- migrate an older, incompatible table definition.
-- All timestamps represent instants (timestamptz); the IANA timezone name lives
-- separately in profiles.timezone. Clients pass aware datetime values.
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    business_stage TEXT,
    industry TEXT,
    legal_form TEXT,
    tax_regime TEXT,
    experience TEXT,
    current_goal TEXT,
    timezone TEXT NOT NULL DEFAULT 'Europe/Moscow'
);

CREATE TABLE IF NOT EXISTS reminders (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('custom', 'learning', 'tax')),
    text TEXT NOT NULL,
    next_run_at TIMESTAMPTZ NOT NULL,
    rrule TEXT,
    active BOOLEAN NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS reminders_due_idx
    ON reminders (next_run_at) WHERE active;
CREATE INDEX IF NOT EXISTS reminders_user_idx ON reminders (user_id);

-- Events are personal history, not an audit log retained after user deletion.
-- Deleting only a profile leaves the user and their history intact.
CREATE TABLE IF NOT EXISTS events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS events_user_history_idx
    ON events (user_id, occurred_at DESC);

-- Materials belong to the knowledge base, so material_id has no database FK.
-- The primary key also indexes the already-shown list for each user.
CREATE TABLE IF NOT EXISTS shown_materials (
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    material_id TEXT NOT NULL,
    shown_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, material_id)
);
