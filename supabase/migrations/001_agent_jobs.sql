-- Foretell agent run queue (shared Supabase project; users table already exists)
CREATE TABLE IF NOT EXISTS agent_jobs (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  run_mode TEXT NOT NULL,
  topic TEXT,
  category TEXT,
  market_id TEXT,
  parent_run_id TEXT,
  force BOOLEAN NOT NULL DEFAULT false,
  card_run_id TEXT,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_agent_jobs_user
  ON agent_jobs (user_id, created_at DESC);
