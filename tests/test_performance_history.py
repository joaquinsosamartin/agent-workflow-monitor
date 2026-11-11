"""Performance sample retention and bounded historical queries, offline."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor.performance import Performance
from monitor.performance_history import PerformanceHistory


class PerformanceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.metrics=Performance(samples=20)
        self.cpu=1.0
        def collect():
            return {"rss_mb":45.25,"cpu_user_seconds":self.cpu,
                    "cpu_system_seconds":0,"threads":6}
        self.sampler=PerformanceHistory(
            Path(self.temp.name)/"performance.db",
            interval_seconds=5,retention_hours=1,
            collect=collect,metrics=self.metrics)

    def tearDown(self):
        self.sampler.stop()
        self.temp.cleanup()

    def test_sample_saves_cpu_memory_and_latency_without_sensitive_content(self):
        self.metrics.observe("observer.scan_ms",3.3)
        self.metrics.observe("sqlite.event_write_ms",1.4)
        self.metrics.count("mod.events_received",2)
        first=self.sampler.sample()
        self.assertIsNone(first["cpu_percent"])
        self.cpu+=0.2
        with patch("monitor.performance_history.time.monotonic",
                   return_value=self.sampler._previous[0]+1):
            second=self.sampler.sample()
        self.assertAlmostEqual(second["cpu_percent"],20.0)
        history=self.sampler.history(hours=1)
        self.assertEqual(history["total_samples"],2)
        self.assertEqual(history["points"][0]["rss_mb"],45.25)
        self.assertEqual(history["points"][-1]["observer_scan_p95_ms"],3.3)
        self.assertEqual(history["points"][-1]["mod_events_received"],2)
        self.assertNotIn("agent",str(history))

    def test_history_limit_is_bounded_and_ordered(self):
        for n in range(24):
            self.cpu+=0.1
            self.sampler.sample()
        history=self.sampler.history(hours=1,limit=10)
        self.assertEqual(history["total_samples"],24)
        self.assertLessEqual(len(history["points"]),10)
        self.assertEqual(
            [p["timestamp"] for p in history["points"]],
            sorted(p["timestamp"] for p in history["points"])
        )

    def test_retention_removes_expired_samples(self):
        self.sampler.sample()
        with self.sampler._lock:
            from contextlib import closing
            with closing(self.sampler._connect()) as conn,conn:
                conn.execute("UPDATE performance_samples SET timestamp='2020-01-01T00:00:00+00:00'")
        # Trigger the periodic cleanup branch.
        self.sampler._iterations=max(1,int(300/self.sampler.interval))-1
        self.sampler.sample()
        result=self.sampler.history(hours=1)
        self.assertEqual(result["total_samples"],1)

    def test_no_raw_event_data_in_schema(self):
        self.sampler.sample()
        with self.sampler._lock:
            from contextlib import closing
            with closing(self.sampler._connect()) as conn:
                columns={row[1] for row in conn.execute("PRAGMA table_info(performance_samples)")}
        self.assertFalse({"prompt","tool_arguments","message","command"} & columns)


if __name__=="__main__":
    unittest.main()
