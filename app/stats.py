import os
import sqlite3
import time
from typing import Any, Dict, List, Optional
from app.config import get_settings
from app.db import _connect_ro

_stats_cache: Dict[str, Any] = {}
STATS_CACHE_TTL = 30.0  # 30s cache TTL


def _get_cached(cache_key: str) -> Optional[Any]:
    if cache_key in _stats_cache:
        entry_time, data = _stats_cache[cache_key]
        if time.time() - entry_time < STATS_CACHE_TTL:
            return data
    return None


def _set_cached(cache_key: str, data: Any) -> None:
    _stats_cache[cache_key] = (time.time(), data)


def get_db_file_size(db_path: str) -> Dict[str, Any]:
    abs_path = os.path.abspath(db_path)
    if os.path.exists(abs_path):
        size_bytes = os.path.getsize(abs_path)
        size_mb = round(size_bytes / (1024 * 1024), 2)
    else:
        size_bytes = 0
        size_mb = 0.0
    return {"bytes": size_bytes, "mb": size_mb}


def get_summary_stats(from_ts: Optional[float] = None, to_ts: Optional[float] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
    settings = get_settings()
    path = db_path or settings.db_path
    cache_key = f"summary:{from_ts}:{to_ts}:{path}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    with _connect_ro(path) as conn:
        where_clauses = []
        params: List[Any] = []
        if from_ts is not None:
            where_clauses.append("created_at >= ?")
            params.append(from_ts)
        if to_ts is not None:
            where_clauses.append("created_at <= ?")
            params.append(to_ts)
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        row = conn.execute(f"""
            SELECT 
                COUNT(*) as total_jobs,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_jobs,
                SUM(CASE WHEN status = 'canceled' THEN 1 ELSE 0 END) as canceled_jobs,
                SUM(COALESCE(input_tokens, 0)) as total_input_tokens,
                SUM(COALESCE(output_tokens, 0)) as total_output_tokens,
                SUM(COALESCE(total_tokens, 0)) as total_tokens,
                SUM(COALESCE(cost_usd, 0.0)) as total_cost_usd
            FROM jobs {where_sql};
        """, params).fetchone()

        d = dict(row) if row else {}
        total = d.get("total_jobs") or 0
        failed = d.get("failed_jobs") or 0
        error_rate = round(failed / total, 4) if total > 0 else 0.0

        durations_rows = conn.execute(f"""
            SELECT (finished_at - started_at) as dur
            FROM jobs
            {where_sql} {"AND" if where_sql else "WHERE"} status = 'completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL
            ORDER BY dur ASC;
        """, params).fetchall()

        durations = [r[0] for r in durations_rows if r[0] is not None]
        if durations:
            n = len(durations)
            p50 = round(durations[int(n * 0.50)], 2)
            p95 = round(durations[min(int(n * 0.95), n - 1)], 2)
            max_dur = round(durations[-1], 2)
        else:
            p50 = p95 = max_dur = 0.0

        db_size = get_db_file_size(path)

        res = {
            "total_jobs": total,
            "completed_jobs": d.get("completed_jobs") or 0,
            "failed_jobs": failed,
            "canceled_jobs": d.get("canceled_jobs") or 0,
            "error_rate": error_rate,
            "duration_p50_s": p50,
            "duration_p95_s": p95,
            "duration_max_s": max_dur,
            "total_input_tokens": d.get("total_input_tokens") or 0,
            "total_output_tokens": d.get("total_output_tokens") or 0,
            "total_tokens": d.get("total_tokens") or 0,
            "total_cost_usd": round(d.get("total_cost_usd") or 0.0, 4),
            "db_size": db_size,
        }

    _set_cached(cache_key, res)
    return res


def get_timeseries_stats(bucket: str = "day", from_ts: Optional[float] = None, to_ts: Optional[float] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    settings = get_settings()
    path = db_path or settings.db_path
    cache_key = f"timeseries:{bucket}:{from_ts}:{to_ts}:{path}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    fmt_map = {
        "hour": "%Y-%m-%d %H:00",
        "day": "%Y-%m-%d",
        "week": "%Y-%W",
        "month": "%Y-%m",
        "year": "%Y",
    }
    date_fmt = fmt_map.get(bucket, "%Y-%m-%d")

    where_clauses = []
    params: List[Any] = []
    if from_ts is not None:
        where_clauses.append("created_at >= ?")
        params.append(from_ts)
    if to_ts is not None:
        where_clauses.append("created_at <= ?")
        params.append(to_ts)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT 
            strftime('{date_fmt}', created_at, 'unixepoch', 'localtime') as time_bucket,
            COUNT(*) as count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as errors,
            SUM(COALESCE(total_tokens, 0)) as total_tokens,
            SUM(COALESCE(cost_usd, 0.0)) as total_cost_usd
        FROM jobs
        {where_sql}
        GROUP BY time_bucket
        ORDER BY time_bucket ASC;
    """

    with _connect_ro(path) as conn:
        rows = conn.execute(sql, params).fetchall()
        res = [dict(r) for r in rows]

    _set_cached(cache_key, res)
    return res


def get_load_stats(by: str = "dow_hour", db_path: Optional[str] = None) -> Dict[str, Any]:
    settings = get_settings()
    path = db_path or settings.db_path
    cache_key = f"load:{by}:{path}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    with _connect_ro(path) as conn:
        if by == "dow":
            # strftime('%w') returns 0=Sunday, 1=Monday, ..., 6=Saturday
            rows = conn.execute("""
                SELECT 
                    CAST(strftime('%w', created_at, 'unixepoch', 'localtime') AS INTEGER) as dow,
                    COUNT(*) as count
                FROM jobs
                GROUP BY dow
                ORDER BY dow ASC;
            """).fetchall()
            dow_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
            data = [{"dow": r[0], "day_name": dow_names[r[0]], "count": r[1]} for r in rows]
            res = {"by": "dow", "data": data}

        elif by == "hour":
            rows = conn.execute("""
                SELECT 
                    CAST(strftime('%H', created_at, 'unixepoch', 'localtime') AS INTEGER) as hour,
                    COUNT(*) as count
                FROM jobs
                GROUP BY hour
                ORDER BY hour ASC;
            """).fetchall()
            data = [{"hour": r[0], "count": r[1]} for r in rows]
            res = {"by": "hour", "data": data}

        else:  # dow_hour (7x24 grid)
            rows = conn.execute("""
                SELECT 
                    CAST(strftime('%w', created_at, 'unixepoch', 'localtime') AS INTEGER) as dow,
                    CAST(strftime('%H', created_at, 'unixepoch', 'localtime') AS INTEGER) as hour,
                    COUNT(*) as count
                FROM jobs
                GROUP BY dow, hour
                ORDER BY dow ASC, hour ASC;
            """).fetchall()
            grid = [[0 for _ in range(24)] for _ in range(7)]
            for r in rows:
                dow, hr, cnt = r[0], r[1], r[2]
                if 0 <= dow < 7 and 0 <= hr < 24:
                    grid[dow][hr] = cnt
            res = {"by": "dow_hour", "grid": grid}

    _set_cached(cache_key, res)
    return res


def get_sources_stats(from_ts: Optional[float] = None, to_ts: Optional[float] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    settings = get_settings()
    path = db_path or settings.db_path
    cache_key = f"sources:{from_ts}:{to_ts}:{path}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    where_clauses = []
    params: List[Any] = []
    if from_ts is not None:
        where_clauses.append("created_at >= ?")
        params.append(from_ts)
    if to_ts is not None:
        where_clauses.append("created_at <= ?")
        params.append(to_ts)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT 
            COALESCE(client_ip, 'unknown') as client_ip,
            COALESCE(auth_mode, 'unknown') as auth_mode,
            COUNT(*) as total_jobs,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_jobs
        FROM jobs
        {where_sql}
        GROUP BY client_ip, auth_mode
        ORDER BY total_jobs DESC;
    """

    with _connect_ro(path) as conn:
        rows = conn.execute(sql, params).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            tot = d["total_jobs"]
            fail = d["failed_jobs"]
            d["error_rate"] = round(fail / tot, 4) if tot > 0 else 0.0
            res.append(d)

    _set_cached(cache_key, res)
    return res


def get_error_stats(from_ts: Optional[float] = None, to_ts: Optional[float] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
    settings = get_settings()
    path = db_path or settings.db_path
    cache_key = f"errors:{from_ts}:{to_ts}:{path}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    where_clauses = ["status = 'failed'"]
    params: List[Any] = []
    if from_ts is not None:
        where_clauses.append("created_at >= ?")
        params.append(from_ts)
    if to_ts is not None:
        where_clauses.append("created_at <= ?")
        params.append(to_ts)
    where_sql = "WHERE " + " AND ".join(where_clauses)

    sql = f"""
        SELECT error, stderr, exit_code
        FROM jobs
        {where_sql};
    """

    taxonomy = {
        "timeout": 0,
        "rate_limited": 0,
        "nonzero_exit": 0,
        "sandbox_error": 0,
        "other": 0,
    }

    with _connect_ro(path) as conn:
        rows = conn.execute(sql, params).fetchall()
        for r in rows:
            err_str = (r["error"] or "") + " " + (r["stderr"] or "")
            code = r["exit_code"]
            if "timed out" in err_str.lower() or code == -1:
                taxonomy["timeout"] += 1
            elif "rate limit" in err_str.lower() or "429" in err_str or "quota" in err_str.lower():
                taxonomy["rate_limited"] += 1
            elif "sandbox" in err_str.lower() or "bwrap" in err_str.lower():
                taxonomy["sandbox_error"] += 1
            elif code is not None and code != 0:
                taxonomy["nonzero_exit"] += 1
            else:
                taxonomy["other"] += 1

    res = {
        "total_errors": len(rows),
        "taxonomy": taxonomy,
    }

    _set_cached(cache_key, res)
    return res
