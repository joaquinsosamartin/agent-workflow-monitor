"""Tests for process discovery without reading host processes or user prompts."""
import unittest
from unittest.mock import patch
from monitor.process_watch import ProcessRegistry, _provider, _resume_id, _session_uuid


class ProcessWatchTests(unittest.TestCase):
    def test_cli_detection(self):
        self.assertEqual(_provider("claude.exe", ["claude", "--resume", "123"]), "claude")
        self.assertEqual(_provider("codex.exe", ["codex", "resume"]), "codex")
        self.assertEqual(_provider("python.exe", ["python", "-c", "print('claude')"]), None)
        self.assertEqual(_provider("node.exe", ["node", "C:/tools/@anthropic-ai/claude-code/cli.js"]), "claude")

    def test_explicit_resume_id(self):
        sid = "12345678-1111-2222-3333-123456789abc"
        self.assertEqual(_resume_id("claude", ["claude", "--resume", sid]), sid)
        self.assertEqual(_resume_id("codex", ["codex", "resume", sid]), sid)
        self.assertIsNone(_resume_id("claude", ["claude", "Tell me about " + sid]))

    def test_source_file_session_ids(self):
        sid = "12345678-1111-2222-3333-123456789abc"
        self.assertEqual(_session_uuid("claude", f"C:/tmp/{sid}.jsonl"), sid)
        self.assertEqual(_session_uuid("codex", f"rollout-2026-11-11T10-00-00-{sid}.jsonl"), sid)
        self.assertEqual(_session_uuid("claude", "unrelated.jsonl"), None)

    def test_unmatched_codex_kept_visible_in_diagnostics(self):
        registry = ProcessRegistry(ttl_seconds=300)
        registry._processes = [
            {"pid": 700, "ppid": 2, "provider": "codex", "session_id": None},
            {"pid": 701, "ppid": 700, "provider": "claude", "session_id": None}
        ]
        import time
        registry._updated = time.monotonic()
        result = registry.classify([], [])
        self.assertEqual(result["processes_by_provider"], {"codex": 1, "claude": 1})
        self.assertEqual(len(result["unmatched_processes"]), 2)
        self.assertEqual(result["process_relationships"][0]["parent_pid"], 700)
        self.assertEqual(result["process_relationships"][0]["child_pid"], 701)

    def test_process_refresh_does_not_block_http_snapshot(self):
        from threading import Event
        import time
        registry = ProcessRegistry(ttl_seconds=30)
        started = Event()
        finish = Event()
        def slow_collect():
            started.set()
            finish.wait(timeout=3)
            return ([{"pid":123,"ppid":0,"provider":"codex","session_id":None}],
                    [], {123:0})
        registry._collect = slow_collect
        try:
            first = registry.snapshot()
            self.assertTrue(first["refreshing"])
            self.assertTrue(started.wait(timeout=1))
            # The refresh thread is still waiting, yet another HTTP caller
            # receives a cached snapshot without waiting for psutil.
            again = registry.snapshot()
            self.assertTrue(again["refreshing"])
            self.assertEqual(again["processes"], [])
        finally:
            finish.set()
        for _ in range(100):
            snap=registry.snapshot()
            if not snap["refreshing"]:
                break
            time.sleep(.01)
        self.assertEqual(snap["processes"][0]["pid"], 123)

    def test_only_explicit_matches_confirm_active(self):
        sid = "12345678-1111-2222-3333-123456789abc"
        registry = ProcessRegistry(ttl_seconds=300)
        registry._processes = [
            {"pid": 101, "provider": "codex", "session_id": sid},
            {"pid": 102, "provider": "codex", "session_id": None}
        ]
        import time
        registry._updated = time.monotonic()
        runs = [
            {"id": "run1", "status": "running", "updated_at": "2026-11-11T10:00:00+00:00"},
            {"id": "run2", "status": "idle", "updated_at": "2026-11-11T10:00:00+00:00"}
        ]
        files = [{"run_id": "run1", "provider": "codex", "path": f"rollout-2026-11-11T10-00-00-{sid}.jsonl"},
                 {"run_id": "run2", "provider": "codex", "path": "rollout-old.jsonl"}]
        result = registry.classify(runs, files)
        self.assertEqual(runs[0]["presence"], "waiting")
        self.assertTrue(runs[0]["process_verified"])
        self.assertEqual(runs[1]["presence"], "inactive")
        self.assertEqual(result["unmatched_processes"][0]["pid"], 102)


if __name__ == "__main__":
    unittest.main()
