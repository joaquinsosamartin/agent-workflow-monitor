"""Claude multi-session grouping: verified, manually linked and usage totals."""
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

from monitor.db import Store


class AgentSessionsTests(unittest.TestCase):
    def setUp(self):
        self.module=importlib.import_module("monitor.app")
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/"monitor.db")
        with self.store.lock,self.store.conn:
            self.store.conn.execute("""
                CREATE TABLE agent_session_links (
                    provider TEXT NOT NULL,pid INTEGER NOT NULL,
                    started_at TEXT NOT NULL,run_id TEXT NOT NULL,
                    linked_at TEXT NOT NULL,PRIMARY KEY(provider,run_id))
            """)
            self.store.conn.execute("""
                CREATE TABLE observer_files (
                    path TEXT PRIMARY KEY,offset INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL,run_id TEXT NOT NULL,last_seen TEXT NOT NULL)
            """)
        self.store.add_run("Claude first","project","r1")
        self.store.add_run("Claude second","project","r2")
        self.store.add_run("Codex other","project","r3")
        with self.store.lock,self.store.conn:
            for rid in ("r1","r2"):
                self.store.conn.execute(
                    "INSERT INTO observer_files VALUES(?,0,'claude',?,'2026-11-12')",
                    (rid+".jsonl",rid))
        self.processes=[
            {"pid":20,"provider":"claude","started_at":"claude-start"},
            {"pid":21,"provider":"claude","started_at":"other-start"},
        ]
        self.store_patch=patch.object(self.module,"store",self.store)
        self.process_patch=patch.object(self.module.process_registry,"snapshot",
            return_value={"processes":self.processes})
        self.store_patch.start();self.process_patch.start()

    def tearDown(self):
        self.process_patch.stop();self.store_patch.stop()
        self.store.close();self.temp.cleanup()

    def test_multiple_claude_sessions_attached_to_one_pid(self):
        self.module._associate_agent_session(self.module.AgentSessionLinkIn(pid=20,run_id="r1"))
        self.module._associate_agent_session(self.module.AgentSessionLinkIn(pid=20,run_id="r2"))
        runs=self.store.run_list()
        sessions=self.module.current_agent_sessions({"processes":self.processes},runs)
        self.assertEqual({r["id"] for r in sessions["20"]},{"r1","r2"})
        self.assertTrue(all(x["association"]=="user_confirmed" for x in sessions["20"]))

    def test_verified_process_session_is_included_automatically(self):
        runs=self.store.run_list()
        for run in runs:
            if run["id"]=="r1":run["pids"]=[20]
        sessions=self.module.current_agent_sessions({"processes":self.processes},runs)
        self.assertEqual(sessions["20"][0]["association"],"verified")

    def test_verified_session_appears_under_only_verified_process(self):
        self.module._associate_agent_session(
            self.module.AgentSessionLinkIn(pid=20,run_id="r1"))
        runs=self.store.run_list()
        for run in runs:
            if run["id"]=="r1":
                run["pids"]=[21]
        sessions=self.module.current_agent_sessions(
            {"processes":self.processes},runs)
        self.assertEqual(sessions["21"][0]["association"],"verified")
        self.assertNotIn("20",sessions)

    def test_codex_sessions_cannot_be_linked_to_claude(self):
        with self.assertRaises(HTTPException):
            self.module._associate_agent_session(self.module.AgentSessionLinkIn(pid=20,run_id="r3"))

    def test_reused_pid_does_not_inherit_session_link(self):
        self.module._associate_agent_session(self.module.AgentSessionLinkIn(pid=20,run_id="r1"))
        process=[{"pid":20,"provider":"claude","started_at":"reused"}]
        self.assertFalse(self.module.current_agent_sessions({"processes":process},
            self.store.run_list()))

    def test_real_usage_is_aggregated_from_known_session_only(self):
        node=self.store.add_node("r1","Task",provider="claude")
        self.store.save_event({
            "type":"usage","run_id":"r1","node_id":node,"provider":"claude",
            "usage":{"session_id":"claude:r1","usage_key":"message:1",
                     "input_tokens":125,"output_tokens":25,"cost_usd":"0.0035",
                     "cost_source":"reported"}
        })
        result=self.module.session_metrics({"r1","r2"})
        self.assertEqual(result["r1"]["tokens"],150)
        self.assertEqual(result["r1"]["cost_usd"],"0.0035")
        self.assertIsNone(result["r2"]["tokens"])
        self.assertEqual(result["r1"]["steps"],1)


if __name__=="__main__":
    unittest.main()
