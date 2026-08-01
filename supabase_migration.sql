-- Supabase Migration SQL
-- Run this in Supabase SQL Editor to create all tables

-- 1. Accounts table
CREATE TABLE IF NOT EXISTS accounts (
  slot_id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_accounts_added_at ON accounts(added_at);

-- 2. Tokens table
CREATE TABLE IF NOT EXISTS tokens (
  id SERIAL PRIMARY KEY,
  payload JSONB NOT NULL,
  added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Recent log table (simplified - no queue fields)
CREATE TABLE IF NOT EXISTS recent_log (
  id SERIAL PRIMARY KEY,
  username TEXT,
  status TEXT,
  error TEXT,
  duration REAL,
  completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_recent_log_completed ON recent_log(completed_at DESC);

-- 4. Site settings table (key-value store for popup/maintenance/theme/layout)
CREATE TABLE IF NOT EXISTS site_settings (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. Proxies table
CREATE TABLE IF NOT EXISTS proxies (
  id SERIAL PRIMARY KEY,
  url TEXT UNIQUE NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_ok_at TIMESTAMPTZ,
  last_err_at TIMESTAMPTZ,
  last_err TEXT
);

-- 6. Mobileconfig history table
CREATE TABLE IF NOT EXISTS mobileconfig_history (
  id SERIAL PRIMARY KEY,
  action TEXT NOT NULL CHECK (action IN ('upload','delete')),
  filename TEXT,
  size INTEGER,
  signed BOOLEAN,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mc_history_created ON mobileconfig_history(created_at DESC);

-- Insert default site settings
INSERT INTO site_settings (key, value, updated_at)
VALUES
  ('theme', '"gold"', NOW()),
  ('layout', '"stacked"', NOW()),
  ('popup', '{"enabled": false, "title": "", "message": "", "icon": "info"}', NOW()),
  ('maintenance', '{"enabled": false, "allow_admin": true}', NOW())
ON CONFLICT (key) DO NOTHING;

-- Success message
SELECT 'Migration completed successfully! All tables created.' as status;
