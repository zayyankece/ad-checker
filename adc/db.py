import json
import sqlite3

from .config import DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  row_idx INTEGER,
  doc_number TEXT,
  docket TEXT,
  issue_date TEXT,
  publish_date TEXT,
  comments_due TEXT,
  make_raw TEXT,
  make_norm TEXT,
  is_airbus INTEGER,
  product_type TEXT,
  subject TEXT,
  doc_type TEXT,
  final_ad_number TEXT,
  supersedes_ad TEXT,
  fr_citation TEXT,
  fr_page_start INTEGER,
  fr_page_end INTEGER,
  url TEXT,
  related_docs TEXT,
  summary TEXT,
  notes TEXT,
  text_source TEXT,
  embedded_text TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  doc_id TEXT,
  section TEXT,
  page INTEGER,
  ord INTEGER,
  text TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED, doc_id UNINDEXED, section, text, tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS revisions (
  doc_id TEXT PRIMARY KEY,
  docket TEXT,
  version_index INTEGER,
  n_versions INTEGER,
  is_latest INTEGER,
  latest_doc_id TEXT,
  docket_status TEXT
);
CREATE TABLE IF NOT EXISTS aircraft (
  registration TEXT PRIMARY KEY,
  model TEXT,
  msn TEXT,
  engine_model TEXT,
  cert_date TEXT,
  mods_installed TEXT,
  parts_installed TEXT,
  facts TEXT
);
CREATE TABLE IF NOT EXISTS signoffs (
  doc_id TEXT,
  registration TEXT,
  system_label TEXT,
  decision TEXT,
  final_label TEXT,
  note TEXT,
  engineer TEXT,
  ts TEXT,
  PRIMARY KEY (doc_id, registration)
);
CREATE TABLE IF NOT EXISTS rules (
  doc_id TEXT PRIMARY KEY,
  rule_json TEXT,
  validation TEXT,
  validation_errors TEXT,
  extractor TEXT,
  approved_by TEXT,
  approved_at TEXT
);
"""


def connect(path=DB):
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for col in ("alt_rule_json TEXT", "alt_extractor TEXT"):
        try:
            con.execute(f"ALTER TABLE rules ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return con


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def one(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def dumps(x):
    return json.dumps(x, ensure_ascii=False)
