-- Telegram Archive Organizer — SQLite schema
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS source_messages(
  id                  INTEGER PRIMARY KEY,
  source_chat_id      INTEGER NOT NULL,
  source_message_id   INTEGER NOT NULL,
  file_id             TEXT,
  filename            TEXT,
  extension           TEXT,
  mime_type           TEXT,
  file_size           INTEGER,
  message_date        INTEGER,
  caption             TEXT,
  media_group_id      TEXT,
  media_type          TEXT,
  message_link        TEXT,
  created_at          TEXT DEFAULT (datetime('now')),
  updated_at          TEXT DEFAULT (datetime('now')),
  UNIQUE(source_chat_id, source_message_id)
);
-- UNIQUE: group_members references source_message_id (FK requires it)
CREATE UNIQUE INDEX IF NOT EXISTS idx_src_msgid ON source_messages(source_message_id);
CREATE INDEX IF NOT EXISTS idx_src_fileid   ON source_messages(file_id);
CREATE INDEX IF NOT EXISTS idx_src_chat     ON source_messages(source_chat_id);

CREATE TABLE IF NOT EXISTS logical_groups(
  id                  INTEGER PRIMARY KEY,
  canonical_name      TEXT NOT NULL,
  normalized_name     TEXT,
  category            TEXT,
  country             TEXT,
  country_code        TEXT,
  country_confidence  REAL,
  country_reasons     TEXT,            -- JSON array
  group_confidence    REAL,
  group_reasons       TEXT,            -- JSON array (explainable decision)
  is_multipart        INTEGER NOT NULL DEFAULT 0,
  part_count          INTEGER,
  total_size          INTEGER,
  status              TEXT NOT NULL DEFAULT 'OK',  -- OK | REVIEW | QUARANTINED
  review_reason       TEXT,
  quarantine_reason   TEXT,
  missing_parts       TEXT,            -- JSON list
  partial_start       INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT DEFAULT (datetime('now')),
  updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_groups_norm    ON logical_groups(normalized_name);
CREATE INDEX IF NOT EXISTS idx_groups_status  ON logical_groups(status);
CREATE INDEX IF NOT EXISTS idx_groups_name    ON logical_groups(canonical_name);

CREATE TABLE IF NOT EXISTS group_members(
  logical_group_id       INTEGER NOT NULL REFERENCES logical_groups(id) ON DELETE CASCADE,
  source_message_id      INTEGER NOT NULL REFERENCES source_messages(source_message_id),
  part_number            INTEGER,
  part_label             TEXT,
  duplicate_of_message_id INTEGER,
  similarity_score       REAL,
  assignment_confidence  REAL,
  PRIMARY KEY (logical_group_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS idx_members_msg ON group_members(source_message_id);

CREATE TABLE IF NOT EXISTS deduplication(
  fingerprint          TEXT NOT NULL,
  fingerprint_type     TEXT NOT NULL,  -- file_id | name_size_part
  canonical_message_id INTEGER NOT NULL,
  duplicate_message_id INTEGER NOT NULL,
  confidence           REAL,
  PRIMARY KEY (fingerprint, duplicate_message_id)
);

CREATE TABLE IF NOT EXISTS destinations(
  id                 INTEGER PRIMARY KEY,
  archive_id         TEXT NOT NULL,
  telegram_chat_id   INTEGER NOT NULL,
  topic_id           INTEGER,
  topic_title        TEXT NOT NULL,
  logical_group_id   INTEGER,
  status             TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | READY
  created_at         TEXT DEFAULT (datetime('now')),
  UNIQUE(archive_id, topic_title)
);
CREATE INDEX IF NOT EXISTS idx_dest_group ON destinations(archive_id, logical_group_id);

CREATE TABLE IF NOT EXISTS processing_jobs(
  id                INTEGER PRIMARY KEY,
  archive_id        TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  logical_group_id  INTEGER,
  operation         TEXT NOT NULL,     -- CREATE_TOPIC | COPY_FILE | POST_SUMMARY
  topic_title       TEXT,
  status            TEXT NOT NULL DEFAULT 'PENDING',
                                  -- PENDING | RUNNING | SUCCESS | FAILED | SKIPPED
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  last_attempt_at   TEXT,
  completed_at      TEXT,
  dest_message_id   INTEGER,
  UNIQUE(archive_id, source_message_id, operation)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON processing_jobs(archive_id, status, logical_group_id);

CREATE TABLE IF NOT EXISTS review_items(
  id                 INTEGER PRIMARY KEY,
  kind               TEXT NOT NULL,    -- grouping | duplicate | country | unresolvable
  logical_group_id   INTEGER,
  source_message_id  INTEGER,
  payload            TEXT,             -- JSON: filenames, proposed group, confidence, reasons...
  recommended_action TEXT,
  status             TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | APPROVED | IGNORED
  action_taken       TEXT,
  created_at         TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(status);

CREATE TABLE IF NOT EXISTS settings(
  key   TEXT PRIMARY KEY,
  value TEXT
);
