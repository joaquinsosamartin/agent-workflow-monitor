"""Bounded in-memory performance telemetry; no prompts or sensitive payloads.

Histograms use a capped ring buffer to keep steady RAM usage. Metrics are
process-local, reset on restart, and are not written to SQLite.
"""
from __future__ import annotations

from collections import defaultdict, deque
from threading import RLock
from time import perf_counter
from contextlib import contextmanager
import os


class Performance:
    def __init__(self, samples=300):
        self.samples = max(10, min(int(samples), 2000))
        self._durations = defaultdict(lambda: deque(maxlen=self.samples))
        self._counters = defaultdict(int)
        self._lock = RLock()

    def observe(self, name: str, duration_ms: float):
        with self._lock:
            self._durations[name].append(max(0.0, float(duration_ms)))

    def count(self, name: str, amount: int = 1):
        with self._lock:
            self._counters[name] += amount

    @contextmanager
    def track(self, name: str):
        begin = perf_counter()
        try:
            yield
        finally:
            self.observe(name, (perf_counter() - begin) * 1000)

    def snapshot(self):
        with self._lock:
            measurements = {}
            for name, values in self._durations.items():
                ordered = sorted(values)
                if not ordered:
                    continue
                def percentile(q):
                    return round(ordered[max(0, min(len(ordered)-1,
                           int((len(ordered)-1)*q)))], 2)
                measurements[name] = {
                    "samples": len(ordered), "p50_ms": percentile(.5),
                    "p95_ms": percentile(.95),
                    "max_ms": round(ordered[-1], 2),
                }
            counters = dict(self._counters)
        return {"latencies": measurements, "counters": counters}


perf = Performance(os.getenv("AGENT_MONITOR_METRIC_SAMPLES", "300"))


def resource_snapshot():
    """Use current-process metrics only; no expensive system-wide process scan."""
    import psutil
    proc = psutil.Process(os.getpid())
    mem = proc.memory_info()
    io = {"rss_mb": round(mem.rss / 1048576, 1),
          "cpu_user_seconds": round(proc.cpu_times().user, 2),
          "cpu_system_seconds": round(proc.cpu_times().system, 2),
          "threads": proc.num_threads()}
    return io
