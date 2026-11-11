"""Synthetic transcript fixtures: no real user session files required."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from monitor.db import Store
from monitor.observer import SessionObserver, stable


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "test.db")
        self.observer = SessionObserver(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, path, *rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def test_claude_usage_and_tool_state_and_resume(self):
        sid="3f70ac12-5108-49ce-9fd7-6e5bc141db1b"
        path=self.root / ".claude" / "projects" / "test" / (sid+".jsonl")
        self.append(path,
            {"type":"assistant","uuid":"m1","cwd":"C:/src/demo","message":{
                "id":"msg1","model":"claude-test","usage":{
                    "input_tokens":100,"output_tokens":20,"cache_read_input_tokens":15},
                "content":[{"type":"tool_use","id":"call1","name":"Read","input":{"file_path":"private.txt"}}]}},
            {"type":"user","uuid":"m2","message":{"content":[
                {"type":"tool_result","tool_use_id":"call1","content":"secret output"}]}})
        self.observer._scan_file("claude",path)
        rid=stable("run","claude",sid)
        snapshot=self.store.snapshot(rid)
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot["usage"]),1)
        self.assertEqual(snapshot["usage"][0]["input_tokens"],100)
        self.assertEqual([n["status"] for n in snapshot["nodes"] if n["title"]=="Tool · Read"],["completed"])
        # Sensitive prompt/tool payloads are never persisted to the event ledger.
        self.assertNotIn("private.txt",str(self.store.event_list(rid)))
        self.assertNotIn("secret output",str(self.store.event_list(rid)))
        event_count=len(self.store.event_list(rid))
        self.observer._scan_file("claude",path)
        self.assertEqual(len(self.store.event_list(rid)),event_count)

    def test_session_titles_and_activity_summary(self):
        from monitor.observer import short_title
        sid="8bc10a98-7c89-4a0a-93e7-07457ac1a078"
        path=self.root/".claude"/"projects"/"demo"/(sid+".jsonl")
        self.append(path,
            {"type":"user","uuid":"u1","cwd":"C:/src/customers","message":{
                "role":"user","content":"Implementar autenticación para la API de clientes"}},
            {"type":"assistant","uuid":"a1","message":{"id":"m1","content":[
                {"type":"tool_use","id":"call1","name":"Task","input":{
                    "description":"Revisar seguridad de los endpoints",
                    "prompt":"Never store this delegated prompt"}}]}})
        self.observer._scan_file("claude",path)
        rid=stable("run","claude",sid)
        snap=self.store.snapshot(rid)
        self.assertEqual(snap["run"]["title"],"Implementar autenticación para la API de clientes")
        self.assertTrue(any(n["title"]=="Delegar · Revisar seguridad de los endpoints" for n in snap["nodes"]))
        self.assertNotIn("Never store this delegated prompt",str(self.store.event_list(rid)))
        self.assertTrue(any(r["id"]==rid and r["activity"]["title"].startswith("Delegar") for r in self.store.run_list()))
        self.assertEqual(short_title("Abre  varios archivos"),"Abre varios archivos")

    def test_recent_events_are_newest_first_with_timestamps(self):
        rid=self.store.add_run("Timeline")
        for n in range(55):
            self.store.save_event({"run_id":rid,"type":"log","message":f"event {n}"})
        recent=self.store.recent_events(rid,limit=3)
        self.assertEqual(len(recent),3)
        self.assertEqual([x["seq"] for x in recent],sorted([x["seq"] for x in recent],reverse=True))
        self.assertIn("event 54",recent[0]["payload"])
        self.assertTrue(all(x["created_at"] for x in recent))

    def test_stale_sessions_outside_lookback_are_marked_idle(self):
        import time
        sid="f6716c2f-6893-4949-9306-d83a0af176a1"
        path=self.root/".claude"/"projects"/"demo"/(sid+".jsonl")
        self.append(path,{"type":"user","uuid":"u1","message":{"content":"Test task"}})
        self.observer._scan_file("claude",path)
        rid=stable("run","claude",sid)
        self.assertEqual(self.store.snapshot(rid)["run"]["status"],"running")
        old=time.time()-24*3600
        os.utime(path,(old,old))
        # Old file no longer eligible for the discovery scan, but its
        # stored observer path must still be reconciled.
        self.observer.scan_once()
        self.assertEqual(self.store.snapshot(rid)["run"]["status"],"idle")

    def test_codex_turn_complete_keeps_session_open(self):
        sid = "12345678-1111-2222-3333-123456789abc"
        path = (self.root / ".codex" / "sessions" / "2026" / "11" / "11" /
                ("rollout-2026-11-11T10-00-00-" + sid + ".jsonl"))
        self.append(path,
            {"type":"session_meta","payload":{"id":sid,"cwd":"C:/src/customer-api"}},
            {"type":"event_msg","timestamp":"first","payload":{"type":"task_started"}},
            {"type":"event_msg","timestamp":"second","payload":{"type":"task_complete"}})
        self.observer._scan_file("codex",path)
        rid = stable("run","codex",path.stem)
        snap = self.store.snapshot(rid)
        self.assertEqual(snap["run"]["status"],"running")
        self.assertEqual(snap["nodes"][0]["status"],"completed")
        self.assertEqual(self.observer.file_stats[str(path)]["invalid_this_scan"],0)

    def test_codex_malformed_record_does_not_hide_session(self):
        sid = "12345678-1111-2222-3333-123456789abc"
        path = (self.root / ".codex" / "sessions" / "2026" / "11" / "11" /
                ("rollout-2026-11-11T10-00-00-" + sid + ".jsonl"))
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open("w",encoding="utf-8") as stream:
            stream.write(json.dumps({"type":"session_meta","payload":{
                "id":sid,"cwd":"C:/src/customer-api"}})+"\\n")
            stream.write("{invalid json}\\n")
            stream.write(json.dumps({"type":"event_msg","timestamp":"t1","payload":{
                "type":"task_started"}})+"\\n")
        self.observer._scan_file("codex",path)
        rid=stable("run","codex",path.stem)
        self.assertIsNotNone(self.store.snapshot(rid))
        self.assertEqual(self.store.snapshot(rid)["run"]["status"],"running")
        self.assertEqual(self.observer.file_stats[str(path)]["invalid_this_scan"],1)

    def test_codex_cumulative_token_usage_is_not_summed(self):
        path=self.root / ".codex" / "sessions" / "2026" / "11" / "11" / "rollout-test.jsonl"
        self.append(path,
            {"type":"session_meta","payload":{"cwd":"C:/src/demo","id":"test-session"}},
            {"type":"event_msg","timestamp":"1","payload":{"type":"token_count","info":{
               "total_token_usage":{"input_tokens":100,"output_tokens":20,"cached_input_tokens":50}}}},
            {"type":"event_msg","timestamp":"2","payload":{"type":"token_count","info":{
               "total_token_usage":{"input_tokens":150,"output_tokens":40,"cached_input_tokens":60}}}})
        self.observer._scan_file("codex",path)
        rid=stable("run","codex",path.stem)
        usage=self.store.snapshot(rid)["usage"]
        self.assertEqual(len(usage),1)
        self.assertEqual(usage[0]["input_tokens"],150)
        self.assertEqual(usage[0]["output_tokens"],40)
        self.assertEqual(usage[0]["cost_source"],"unavailable")


if __name__=="__main__":
    unittest.main()
