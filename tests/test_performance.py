"""Low-overhead performance telemetry tests (no network or GitHub Actions)."""
import time
import unittest

from monitor.performance import Performance


class PerformanceTests(unittest.TestCase):
    def test_quantiles_and_bounded_window(self):
        metrics=Performance(samples=10)
        for value in range(30):
            metrics.observe("hook.ms",value)
        state=metrics.snapshot()
        latency=state["latencies"]["hook.ms"]
        self.assertEqual(latency["samples"],10)
        self.assertEqual(latency["max_ms"],29)
        self.assertGreaterEqual(latency["p95_ms"],latency["p50_ms"])

    def test_counter_and_timed_scope(self):
        metrics=Performance()
        metrics.count("event.accepted",2)
        with metrics.track("operation.ms"):
            time.sleep(0.001)
        result=metrics.snapshot()
        self.assertEqual(result["counters"]["event.accepted"],2)
        self.assertEqual(result["latencies"]["operation.ms"]["samples"],1)

    def test_negative_durations_are_clamped(self):
        metrics=Performance()
        metrics.observe("invalid.ms",-15)
        self.assertEqual(metrics.snapshot()["latencies"]["invalid.ms"]["max_ms"],0)


if __name__=="__main__":
    unittest.main()
