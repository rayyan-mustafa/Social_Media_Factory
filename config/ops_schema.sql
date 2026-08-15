# SQL schema reference for future Postgres migration (ops agents).
# File-backed store in output/ops/ is the v1 default.

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  sheet_row INT,
  video_id TEXT,
  watch_url TEXT,
  job_dir TEXT,
  error TEXT,
  meta JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  job_id TEXT,
  agent TEXT NOT NULL,
  event_type TEXT NOT NULL,
  severity TEXT,
  message TEXT,
  action TEXT,
  payload JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS policy_snapshots (
  version INT,
  content_hash TEXT,
  fetched_at TIMESTAMPTZ,
  sources JSONB,
  rule_pack JSONB,
  compile_ok BOOLEAN
);

CREATE TABLE IF NOT EXISTS algo_insights (
  id TEXT PRIMARY KEY,
  video_id TEXT,
  title TEXT,
  metrics JSONB,
  vs_benchmark JSONB,
  proposals JSONB,
  created_at TIMESTAMPTZ
);
