import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from app.config import get_settings

_db_lock = threading.Lock()


def _get_db_path(db_path: Optional[str] = None) -> str:
    if db_path is not None:
        return db_path
    settings = get_settings()
    return settings.db_path


def _connect(path: str) -> sqlite3.Connection:
    dirname = os.path.dirname(os.path.abspath(path))
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    if d.get("metadata") is not None and isinstance(d["metadata"], str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except json.JSONDecodeError:
            pass
    return d


def init_db(db_path: Optional[str] = None) -> None:
    path = _get_db_path(db_path)
    with _db_lock:
        with _connect(path) as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                model TEXT,
                effort TEXT,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                workdir TEXT,
                exit_code INTEGER,
                stdout TEXT,
                stderr TEXT,
                error TEXT,
                metadata TEXT,
                wait INTEGER NOT NULL DEFAULT 60,
                timeout INTEGER NOT NULL DEFAULT 120,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );
            """)
            # Migration check for existing databases
            cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs);").fetchall()]
            if "effort" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN effort TEXT;")
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_status_next_attempt 
            ON jobs(status, next_attempt_at, created_at);
            """)
            conn.commit()



def create_job(
    agent: str,
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    wait: int = 60,
    timeout: int = 120,
    db_path: Optional[str] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    path = _get_db_path(db_path)
    job_id = job_id or str(uuid.uuid4())

    now = time.time()
    meta_json = json.dumps(metadata) if metadata is not None else None

    with _db_lock:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs (
                    id, agent, model, effort, prompt, status, attempts, next_attempt_at,
                    metadata, wait, timeout, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)
                RETURNING *;
                """,
                (job_id, agent, model, effort, prompt, now, meta_json, wait, timeout, now),
            )
            row = cur.fetchone()
            conn.commit()
            return _row_to_dict(row)



def claim_next_job(now: Optional[float] = None, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = _get_db_path(db_path)
    current_time = now if now is not None else time.time()

    with _db_lock:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    started_at = ?,
                    attempts = attempts + 1
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = 'pending' AND next_attempt_at <= ?
                    ORDER BY created_at ASC LIMIT 1
                )
                RETURNING *;
                """,
                (current_time, current_time),
            )
            row = cur.fetchone()
            conn.commit()
            return _row_to_dict(row)


def finish_job(
    job_id: str,
    status: str,
    exit_code: Optional[int] = None,
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    error: Optional[str] = None,
    finished_at: Optional[float] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    path = _get_db_path(db_path)
    end_time = finished_at if finished_at is not None else time.time()

    with _db_lock:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN status = 'canceled' THEN 'canceled' ELSE ? END,
                    exit_code = ?,
                    stdout = ?,
                    stderr = ?,
                    error = ?,
                    finished_at = ?
                WHERE id = ?
                RETURNING *;
                """,
                (status, exit_code, stdout, stderr, error, end_time, job_id),

            )
            row = cur.fetchone()
            conn.commit()
            return _row_to_dict(row)


def schedule_retry(
    job_id: str,
    next_attempt_at: float,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    path = _get_db_path(db_path)

    with _db_lock:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'pending',
                    next_attempt_at = ?
                WHERE id = ?
                RETURNING *;
                """,
                (next_attempt_at, job_id),
            )
            row = cur.fetchone()
            conn.commit()
            return _row_to_dict(row)


def cancel_job(job_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = _get_db_path(db_path)
    now = time.time()

    with _db_lock:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'canceled',
                    finished_at = ?
                WHERE id = ?
                RETURNING *;
                """,
                (now, job_id),
            )
            row = cur.fetchone()
            conn.commit()
            return _row_to_dict(row)


def get_job(job_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = _get_db_path(db_path)

    with _db_lock:
        with _connect(path) as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            return _row_to_dict(row)


def list_jobs(
    status: Optional[str] = None,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    path = _get_db_path(db_path)

    with _db_lock:
        with _connect(path) as conn:
            if status is not None:
                cur = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?;",
                    (status, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?;",
                    (limit,),
                )
            rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]


def reset_orphans(db_path: Optional[str] = None) -> int:
    path = _get_db_path(db_path)

    with _db_lock:
        with _connect(path) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'pending' WHERE status = 'running';"
            )
            count = cur.rowcount
            conn.commit()
            return count


def purge_old(before_ts: float, db_path: Optional[str] = None) -> int:
    path = _get_db_path(db_path)

    with _db_lock:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                DELETE FROM jobs 
                WHERE status IN ('completed', 'failed', 'canceled')
                  AND (finished_at < ? OR (finished_at IS NULL AND created_at < ?));
                """,
                (before_ts, before_ts),
            )
            count = cur.rowcount
            conn.commit()
            return count
