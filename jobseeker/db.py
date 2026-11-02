"""SQLite storage. Jobs are kept across runs so each posting is scored and tailored once."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    description TEXT,
    posted_at TEXT,
    exp_min INTEGER,
    exp_max INTEGER,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    fit_score INTEGER,
    bucket TEXT,
    fit_json TEXT,
    resume_hash TEXT,
    tailored_path TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    active_state TEXT NOT NULL DEFAULT 'unverified',
    verified_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary_json TEXT,
    report_path TEXT
);
CREATE TABLE IF NOT EXISTS analysis (
    resume_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    analysis_json TEXT NOT NULL
);
"""


@contextmanager
def connect():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "active_state" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN active_state TEXT NOT NULL DEFAULT 'unverified'")
        conn.execute("ALTER TABLE jobs ADD COLUMN verified_at TEXT")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def upsert_job(conn, job) -> bool:
    """Store a posting; True when it hasn't been seen before."""
    existing = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,)).fetchone()
    if existing:
        # Being listed right now proves the posting is open, including one that reopened.
        conn.execute("UPDATE jobs SET last_seen = ?, active_state = 'active', verified_at = ? WHERE id = ?",
                     (now(), now(), job.id))
        return False
    conn.execute(
        "INSERT INTO jobs (id, company, title, location, url, source, description, posted_at,"
        " exp_min, exp_max, first_seen, last_seen, active_state, verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
        (job.id, job.company, job.title, job.location, job.url, job.source, job.description,
         job.posted_at, job.exp_min, job.exp_max, now(), now(), now()),
    )
    return True


def save_fit(conn, job_id: str, fit: dict, resume_hash: str) -> None:
    conn.execute(
        "UPDATE jobs SET fit_score = ?, bucket = ?, fit_json = ?, resume_hash = ? WHERE id = ?",
        (fit["fit_score"], fit["bucket"], json.dumps(fit), resume_hash, job_id),
    )
