-- Interview Assistant Schema
-- Paste this into Supabase SQL Editor and click "Run"

CREATE TABLE IF NOT EXISTS licenses (
    key TEXT PRIMARY KEY,
    hwid TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    current_period_end BIGINT DEFAULT 0,
    created BIGINT DEFAULT 0,
    email TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    key TEXT,
    status TEXT,
    raw TEXT,
    ts BIGINT
);

CREATE TABLE IF NOT EXISTS telegram_connections (
    chat_id TEXT PRIMARY KEY,
    bot_token TEXT NOT NULL,
    user_hwid TEXT DEFAULT '',
    license_key TEXT DEFAULT '',
    connected_at BIGINT DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_licenses_hwid ON licenses(hwid);
CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses(status);
CREATE INDEX IF NOT EXISTS idx_payments_key ON payments(key);
CREATE INDEX IF NOT EXISTS idx_telegram_hwid ON telegram_connections(user_hwid);
