import asyncio
import json
import logging
import logging.handlers
import os
import queue
import sqlite3
import time
from typing import Any, Dict, List, Optional
from app.config import get_settings

_EVENTS_DB_LOCK = __import__("threading").Lock()


def _get_events_db_path(db_path: Optional[str] = None) -> str:
    if db_path is not None:
        return db_path
    settings = get_settings()
    events_path = os.environ.get("LOG_EVENTS_DB", os.path.join(os.path.dirname(settings.db_path), "events.db"))
    return events_path


def _connect_events(path: str) -> sqlite3.Connection:
    dirname = os.path.dirname(os.path.abspath(path))
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _connect_events_ro(path: str) -> sqlite3.Connection:
    abs_path = os.path.abspath(path)
    uri_path = f"file:{abs_path}?mode=ro"
    conn = sqlite3.connect(uri_path, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_events_db(db_path: Optional[str] = None) -> None:
    path = _get_events_db_path(db_path)
    with _EVENTS_DB_LOCK:
        with _connect_events(path) as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                level TEXT NOT NULL,
                logger TEXT NOT NULL,
                job_id TEXT,
                message TEXT NOT NULL,
                extra TEXT
            );
            """)
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_ts_level ON events(ts, level);
            """)
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id);
            """)
            conn.commit()


class SQLiteEventsHandler(logging.Handler):
    """Custom logging Handler that writes records into events.db."""
    def __init__(self, db_path: Optional[str] = None):
        super().__init__()
        self.db_path = _get_events_db_path(db_path)
        init_events_db(self.db_path)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = record.created
            level = record.levelname
            logger_name = record.name
            msg = record.getMessage()

            job_id = getattr(record, "job_id", None)
            if not job_id and "Job " in msg:
                parts = msg.split()
                for p in parts:
                    if len(p) == 36 and p.count("-") == 4:
                        job_id = p
                        break

            extra_dict = {}
            if hasattr(record, "extra") and isinstance(record.extra, dict):
                extra_dict = record.extra
            extra_json = json.dumps(extra_dict) if extra_dict else None

            with _EVENTS_DB_LOCK:
                with _connect_events(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO events (ts, level, logger, job_id, message, extra) VALUES (?, ?, ?, ?, ?, ?)",
                        (ts, level, logger_name, job_id, msg, extra_json)
                    )
                    conn.commit()
        except Exception:
            self.handleError(record)


_log_queue: Optional[queue.Queue] = None
_queue_listener: Optional[logging.handlers.QueueListener] = None


def stop_async_logging() -> None:
    global _queue_listener
    if _queue_listener is not None:
        listener = _queue_listener
        _queue_listener = None
        try:
            listener.enqueue(listener.sentinel)
            if listener._thread and listener._thread.is_alive():
                listener._thread.join(timeout=2.0)
        except Exception:
            pass

    root_logger = logging.getLogger()
    root_logger.handlers = [h for h in root_logger.handlers if not isinstance(h, logging.handlers.QueueHandler)]



def setup_async_logging(log_level: str = "INFO", db_path: Optional[str] = None) -> None:
    stop_async_logging()

    global _log_queue, _queue_listener
    _log_queue = queue.Queue(-1)
    sqlite_handler = SQLiteEventsHandler(db_path=db_path)
    
    level_num = getattr(logging, log_level.upper(), logging.INFO)
    sqlite_handler.setLevel(level_num)

    _queue_listener = logging.handlers.QueueListener(_log_queue, sqlite_handler, respect_handler_level=True)
    _queue_listener.start()

    root_logger = logging.getLogger()
    queue_handler = logging.handlers.QueueHandler(_log_queue)
    queue_handler.setLevel(level_num)

    root_logger.addHandler(queue_handler)



def get_logs(
    level: Optional[str] = None,
    since: Optional[float] = None,
    job_id: Optional[str] = None,
    limit: int = 100,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    path = _get_events_db_path(db_path)
    where_clauses = []
    params: List[Any] = []

    if level:
        where_clauses.append("level = ?")
        params.append(level.upper())
    if since is not None:
        where_clauses.append("ts >= ?")
        params.append(since)
    if job_id:
        where_clauses.append("job_id = ?")
        params.append(job_id)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params.append(limit)

    sql = f"""
        SELECT id, ts, level, logger, job_id, message, extra
        FROM events
        {where_sql}
        ORDER BY ts DESC LIMIT ?;
    """

    with _connect_events_ro(path) as conn:
        rows = conn.execute(sql, params).fetchall()
        res = [dict(r) for r in rows]
    return res


def purge_old_events(cutoff_ts: float, db_path: Optional[str] = None) -> int:
    path = _get_events_db_path(db_path)
    with _EVENTS_DB_LOCK:
        with _connect_events(path) as conn:
            cur = conn.execute("DELETE FROM events WHERE ts < ?;", (cutoff_ts,))
            conn.commit()
            return cur.rowcount
