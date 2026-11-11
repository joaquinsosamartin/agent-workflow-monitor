"""Low-overhead process-local performance sampling with bounded SQLite retention.

Samples contain only infrastructure metrics, never prompts, messages, tool
arguments or environment variables. The sampler is independent of the
conversation database locks and uses a single short INSERT transaction.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .performance import perf, resource_snapshot

DEFAULT_INTERVAL = 15
DEFAULT_RETENTION_HOURS = 48
MAX_POINTS = 500

def setting(name, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default

def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class PerformanceHistory:
    def __init__(self, path, interval_seconds=None, retention_hours=None,
                 collect=resource_snapshot, metrics=perf):
        self.path=Path(path)
        self.interval=interval_seconds or setting(
            "AGENT_MONITOR_PERF_INTERVAL_SECONDS", DEFAULT_INTERVAL, 5, 300)
        self.retention_hours=retention_hours or setting(
            "AGENT_MONITOR_PERF_RETENTION_HOURS", DEFAULT_RETENTION_HOURS, 1, 168)
        self.collect=collect
        self.metrics=metrics
        self._thread=None
        self._stop=threading.Event()
        self._lock=threading.RLock()
        self._previous=None
        self.last_error=None
        self.last_sample=None
        self._iterations=0

    def _connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(self.path, timeout=3)
        conn.row_factory=sqlite3.Row
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS performance_samples(
                id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                cpu_percent REAL,
                rss_mb REAL NOT NULL,
                threads INTEGER NOT NULL,
                observer_scan_p95_ms REAL,
                sqlite_write_p95_ms REAL,
                dashboard_p95_ms REAL,
                hook_p95_ms REAL,
                mod_events_received INTEGER NOT NULL,
                mod_events_written INTEGER NOT NULL,
                api_timeouts INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_perf_samples_time
            ON performance_samples(timestamp)
        """)
        conn.commit()
        return conn

    def sample(self):
        """One sample; safe to invoke in tests without running a thread."""
        started=time.monotonic()
        process=self.collect()
        state=self.metrics.snapshot()
        counters=state.get("counters",{})
        latency=state.get("latencies",{})
        previous=self._previous
        cpu_total=float(process.get("cpu_user_seconds",0)) + float(process.get("cpu_system_seconds",0))
        cpu_pct=None
        if previous is not None:
            dt=started-previous[0]
            if dt>0:
                cpu_pct=round(max(0.0, (cpu_total-previous[1])*100.0/dt),2)
        self._previous=(started,cpu_total)
        def p95(name):
            item=latency.get(name)
            return item.get("p95_ms") if item else None
        row={
            "timestamp":iso_now(),
            "cpu_percent":cpu_pct,
            "rss_mb":float(process.get("rss_mb",0)),
            "threads":int(process.get("threads",0)),
            "observer_scan_p95_ms":p95("observer.scan_ms"),
            "sqlite_write_p95_ms":p95("sqlite.event_write_ms"),
            "dashboard_p95_ms":p95("api.dashboard_ms"),
            "hook_p95_ms":p95("mod.hook_overhead_ms"),
            "mod_events_received":int(counters.get("mod.events_received",0)),
            "mod_events_written":int(counters.get("mod.events_written",0)),
            "api_timeouts":int(counters.get("api.timeout",0)),
        }
        with self._lock:
            with self._connect() as conn:
                fields=",".join(row)
                placeholders=",".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO performance_samples({fields}) VALUES({placeholders})",
                    tuple(row.values()))
                self._iterations+=1
                if self._iterations % max(1, int(300/self.interval)) == 0:
                    cutoff=datetime.fromtimestamp(
                        time.time()-self.retention_hours*3600,
                        timezone.utc).isoformat(timespec="seconds")
                    conn.execute("DELETE FROM performance_samples WHERE timestamp < ?",(cutoff,))
        self.last_sample=row["timestamp"]
        self.last_error=None
        return row

    def history(self, hours=1, limit=MAX_POINTS):
        hours=max(1,min(168,int(hours)))
        limit=max(10,min(MAX_POINTS,int(limit)))
        cutoff=datetime.fromtimestamp(time.time()-hours*3600,timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            with self._connect() as conn:
                # Fetch a uniformly distributed bounded set, newest first,
                # without materializing the entire history into Python.
                total=conn.execute(
                    "SELECT COUNT(*) FROM performance_samples WHERE timestamp>=?",
                    (cutoff,)).fetchone()[0]
                stride=max(1,(total+limit-1)//limit)
                rows=conn.execute("""
                    SELECT * FROM (
                        SELECT *,ROW_NUMBER() OVER (ORDER BY timestamp DESC,id DESC) AS rn
                        FROM performance_samples WHERE timestamp>=?
                    ) WHERE (rn-1)%?=0 ORDER BY timestamp,id LIMIT ?
                """,(cutoff,stride,limit)).fetchall()
        return {"hours":hours,"total_samples":total,"interval_seconds":self.interval,
                "retention_hours":self.retention_hours,
                "points":[{k:row[k] for k in row.keys() if k not in ("id","rn")} for row in rows]}

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception as exc:
                self.last_error=f"{type(exc).__name__}: {str(exc)[:160]}"
            self._stop.wait(self.interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread=threading.Thread(target=self._run,daemon=True,
                                     name="agent-monitor-performance")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
