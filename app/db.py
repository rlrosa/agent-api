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


def _connect_ro(path: str) -> sqlite3.Connection:
    abs_path = os.path.abspath(path)
    uri_path = f"file:{abs_path}?mode=ro"
    conn = sqlite3.connect(uri_path, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
                finished_at REAL,
                client_ip TEXT,
                auth_mode TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cached_tokens INTEGER,
                total_tokens INTEGER,
                cost_usd REAL,
                api_key_name TEXT
            );
            """)
            # Migration check for existing databases
            cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs);").fetchall()]
            new_cols = {
                "effort": "TEXT",
                "client_ip": "TEXT",
                "auth_mode": "TEXT",
                "input_tokens": "INTEGER",
                "output_tokens": "INTEGER",
                "cached_tokens": "INTEGER",
                "total_tokens": "INTEGER",
                "cost_usd": "REAL",
                "api_key_name": "TEXT",
            }
            for col_name, col_type in new_cols.items():
                if col_name not in cols:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type};")

            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_status_next_attempt 
            ON jobs(status, next_attempt_at, created_at);
            """)
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_created_at 
            ON jobs(created_at);
            """)
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_status 
            ON jobs(status);
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS job_daily (
                day TEXT NOT NULL,
                agent TEXT NOT NULL,
                hour_bucket INTEGER NOT NULL,
                total_jobs INTEGER NOT NULL DEFAULT 0,
                completed_jobs INTEGER NOT NULL DEFAULT 0,
                failed_jobs INTEGER NOT NULL DEFAULT 0,
                canceled_jobs INTEGER NOT NULL DEFAULT 0,
                sum_input_tokens INTEGER NOT NULL DEFAULT 0,
                sum_output_tokens INTEGER NOT NULL DEFAULT 0,
                sum_total_tokens INTEGER NOT NULL DEFAULT 0,
                sum_cost_usd REAL NOT NULL DEFAULT 0.0,
                duration_p50 REAL NOT NULL DEFAULT 0.0,
                duration_p95 REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY (day, agent, hour_bucket)
            );
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
    client_ip: Optional[str] = None,
    auth_mode: Optional[str] = None,
    api_key_name: Optional[str] = None,
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
                    metadata, wait, timeout, created_at, client_ip, auth_mode, api_key_name
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *;
                """,
                (job_id, agent, model, effort, prompt, now, meta_json, wait, timeout, now, client_ip, auth_mode, api_key_name),
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
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cached_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
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
                    finished_at = ?,
                    input_tokens = ?,
                    output_tokens = ?,
                    cached_tokens = ?,
                    total_tokens = ?,
                    cost_usd = ?
                WHERE id = ?
                RETURNING *;
                """,
                (
                    status,
                    exit_code,
                    stdout,
                    stderr,
                    error,
                    end_time,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    total_tokens,
                    cost_usd,
                    job_id,
                ),
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


def sweep_stuck_running_jobs(db_path: Optional[str] = None) -> int:
    path = _get_db_path(db_path)
    now = time.time()
    stuck_count = 0

    with _db_lock:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                SELECT id, timeout, started_at FROM jobs
                WHERE status = 'running' AND started_at IS NOT NULL;
                """
            )
            rows = cur.fetchall()
            for r in rows:
                job_id = r["id"]
                timeout_val = r["timeout"] or 120
                started_at = r["started_at"]
                if started_at and (now - started_at) > (timeout_val + 15):
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed',
                            exit_code = -1,
                            error = ?,
                            finished_at = ?
                        WHERE id = ? AND status = 'running';
                        """,
                        (f"Job timed out and was swept after {int(now - started_at)}s", now, job_id),
                    )
                    stuck_count += 1
            conn.commit()
    return stuck_count


def build_daily_rollups(target_day: Optional[str] = None, db_path: Optional[str] = None) -> int:
    path = _get_db_path(db_path)
    with _db_lock:
        with _connect(path) as conn:
            where_sql = "WHERE strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') = ?" if target_day else ""
            params = [target_day] if target_day else []

            sql = f"""
                SELECT 
                    strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') as day,
                    agent,
                    CAST(strftime('%H', created_at, 'unixepoch', 'localtime') AS INTEGER) as hour_bucket,
                    COUNT(*) as total_jobs,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_jobs,
                    SUM(CASE WHEN status = 'canceled' THEN 1 ELSE 0 END) as canceled_jobs,
                    SUM(COALESCE(input_tokens, 0)) as sum_input_tokens,
                    SUM(COALESCE(output_tokens, 0)) as sum_output_tokens,
                    SUM(COALESCE(total_tokens, 0)) as sum_total_tokens,
                    SUM(COALESCE(cost_usd, 0.0)) as sum_cost_usd
                FROM jobs
                {where_sql}
                GROUP BY day, agent, hour_bucket;
            """
            rows = conn.execute(sql, params).fetchall()
            count = 0
            for r in rows:
                day, agent, hr = r["day"], r["agent"], r["hour_bucket"]
                dur_rows = conn.execute("""
                    SELECT (finished_at - started_at) as dur
                    FROM jobs
                    WHERE strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') = ?
                      AND agent = ?
                      AND CAST(strftime('%H', created_at, 'unixepoch', 'localtime') AS INTEGER) = ?
                      AND status = 'completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL
                    ORDER BY dur ASC;
                """, (day, agent, hr)).fetchall()
                durs = [dr[0] for dr in dur_rows if dr[0] is not None]
                if durs:
                    n = len(durs)
                    p50 = round(durs[int(n * 0.50)], 2)
                    p95 = round(durs[min(int(n * 0.95), n - 1)], 2)
                else:
                    p50 = p95 = 0.0

                conn.execute("""
                    INSERT INTO job_daily (
                        day, agent, hour_bucket, total_jobs, completed_jobs, failed_jobs, canceled_jobs,
                        sum_input_tokens, sum_output_tokens, sum_total_tokens, sum_cost_usd, duration_p50, duration_p95
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, agent, hour_bucket) DO UPDATE SET
                        total_jobs = excluded.total_jobs,
                        completed_jobs = excluded.completed_jobs,
                        failed_jobs = excluded.failed_jobs,
                        canceled_jobs = excluded.canceled_jobs,
                        sum_input_tokens = excluded.sum_input_tokens,
                        sum_output_tokens = excluded.sum_output_tokens,
                        sum_total_tokens = excluded.sum_total_tokens,
                        sum_cost_usd = excluded.sum_cost_usd,
                        duration_p50 = excluded.duration_p50,
                        duration_p95 = excluded.duration_p95;
                """, (
                    day, agent, hr,
                    r["total_jobs"], r["completed_jobs"], r["failed_jobs"], r["canceled_jobs"],
                    r["sum_input_tokens"], r["sum_output_tokens"], r["sum_total_tokens"], r["sum_cost_usd"],
                    p50, p95
                ))
                count += 1
            conn.commit()
            return count


