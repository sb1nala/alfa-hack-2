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

-- Durable confirmation; a draft is never read by due() or the delivery worker.
CREATE TABLE IF NOT EXISTS reminder_drafts (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    run_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'confirmed', 'cancelled', 'expired')),
    reminder_id BIGINT REFERENCES reminders(id) ON DELETE SET NULL
);
-- Canonical recurrence (DTSTART + RRULE) fixed at draft creation; NULL for a one-off.
ALTER TABLE reminder_drafts ADD COLUMN IF NOT EXISTS rrule TEXT;

-- One event per original scheduled instant, independent of delivery attempts.
-- xmin of the reminder is captured as an optimistic row version, never exposed
-- through SELECT * -> Reminder. Any intervening UPDATE invalidates completion.
CREATE TABLE IF NOT EXISTS reminder_deliveries (
    reminder_id BIGINT NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    scheduled_at TIMESTAMPTZ NOT NULL,
    revision TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('sending', 'retry', 'sent', 'failed', 'obsolete')),
    token UUID NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    retry_at TIMESTAMPTZ NOT NULL,
    sent_at TIMESTAMPTZ,
    message_id BIGINT,
    error_code TEXT,
    PRIMARY KEY (reminder_id, scheduled_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS reminder_one_sender_idx
    ON reminder_deliveries (reminder_id) WHERE state = 'sending';
CREATE INDEX IF NOT EXISTS reminder_retry_idx
    ON reminder_deliveries (retry_at) WHERE state = 'retry';

-- A Forbidden response suppresses ALL further reminders for this recipient,
-- including future creations, until an operator explicitly clears this row.
CREATE TABLE IF NOT EXISTS reminder_blocked_users (
    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    blocked_at TIMESTAMPTZ NOT NULL,
    error_code TEXT NOT NULL
);

-- One current UI session per owner. Replacing the UUID invalidates every old
-- menu; revision makes a step single-use. No UI columns belong in profiles.
CREATE TABLE IF NOT EXISTS profile_ui_sessions (
    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    token UUID NOT NULL UNIQUE,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    mode TEXT NOT NULL CHECK (mode IN ('idle', 'offer', 'home', 'onboarding', 'menu', 'edit', 'delete')),
    field TEXT CHECK (field IN ('business_stage', 'industry', 'legal_form', 'tax_regime',
                               'experience', 'current_goal', 'timezone')),
    expects_text BOOLEAN NOT NULL DEFAULT false,
    onboarding_status TEXT NOT NULL DEFAULT 'new'
        CHECK (onboarding_status IN ('new', 'offered', 'completed', 'skipped')),
    expires_at TIMESTAMPTZ NOT NULL
);
