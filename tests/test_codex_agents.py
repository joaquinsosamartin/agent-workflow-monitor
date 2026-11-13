"""Codex subagent detection from explicit rollout metadata, not Windows PIDs."""
import tempfile
import unittest
import json
from pathlib import Path

from monitor.db import Store
from monitor.observer import SessionObserver, stable
from monitor.codex_agents import (
    source_parent, snapshot as codex_snapshot, collect_descendants,
)


class CodexAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/"monitor.db")
        self.observer=SessionObserver(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def rollout(self,thread_id,source=None,role=None):
        folder=self.root/"sessions"/"2026"/"11"/"13"
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/("rollout-2026-11-13T10-00-00-"+thread_id+".jsonl")
        data={"id":thread_id,"cwd":"C:/src/demo"}
        if source:data["source"]=source
        if role:data["agent_role"]=role
        with path.open("w",encoding="utf-8") as f:
            f.write(json.dumps({"type":"session_meta","payload":data})+"\n")
        return path

    def test_two_level_codex_agents_without_separate_processes(self):
        root="11111111-1111-4111-8111-111111111111"
        child="22222222-2222-4222-8222-222222222222"
        grandchild="33333333-3333-4333-8333-333333333333"
        parent=self.rollout(root)
        second=self.rollout(child,{"subagent":{"thread_spawn":{
            "parent_thread_id":root,"agent_nickname":"Reviewer",
            "agent_role":"explorer","task_name":"Review API"}}})
        third=self.rollout(grandchild,{"subagent":{"thread_spawn":{
            "parent_thread_id":child,"agent_nickname":"Tests"}}})
        # Scan child before parent to prove no ordering requirement.
        for file in (third,second,parent):
            self.observer._scan_file("codex",file)
        tree=codex_snapshot(self.store,self.store.run_list())
        self.assertEqual(tree["known_threads"],3)
        self.assertEqual(tree["subagents"],2)
        root_node=next(r for r in tree["roots"] if r["thread_id"]==root)
        children=collect_descendants(root_node)
        self.assertEqual([c["thread_id"] for c in children],[child,grandchild])
        self.assertEqual(children[0]["role"],"explorer")
        self.assertEqual(children[0]["title"],"Review API")

    def test_subagent_activity_seen_before_child_rollout(self):
        root="11111111-1111-4111-8111-111111111111"
        child="22222222-2222-4222-8222-222222222222"
        parent=self.rollout(root)
        with parent.open("a",encoding="utf-8") as stream:
            stream.write(json.dumps({"type":"event_msg","payload":{
                "type":"sub_agent_activity","kind":"started",
                "agent_thread_id":child,
                "agent_path":"/root/reviewer"}})+"\n")
        self.observer._scan_file("codex",parent)
        tree=codex_snapshot(self.store,self.store.run_list())
        root_node=next(x for x in tree["roots"] if x["thread_id"]==root)
        self.assertEqual(len(root_node["children"]),1)
        placeholder=root_node["children"][0]
        self.assertEqual(placeholder["thread_id"],child)
        self.assertEqual(placeholder["activity"],"started")
        self.assertFalse(placeholder["run_id"])
        child_path=self.rollout(child,{"subagent":{"thread_spawn":{
            "parent_thread_id":root,"agent_nickname":"Reviewer"}}})
        self.observer._scan_file("codex",child_path)
        new=codex_snapshot(self.store,self.store.run_list())
        sub=next(x for x in new["roots"] if x["thread_id"]==root)["children"][0]
        self.assertTrue(sub["run_id"])
        self.assertEqual(sub["nickname"],"Reviewer")
        self.assertEqual(sub["activity"],"started")

    def test_backfill_existing_rollout_without_resetting_offset(self):
        root="11111111-1111-4111-8111-111111111111"
        child="22222222-2222-4222-8222-222222222222"
        file=self.rollout(child,{"subagent":{"thread_spawn":{
            "parent_thread_id":root}}})
        self.observer._scan_file("codex",file)
        first=codex_snapshot(self.store,self.store.run_list())
        self.assertEqual(first["known_threads"],1)
        self.observer._scan_file("codex",file)
        second=codex_snapshot(self.store,self.store.run_list())
        self.assertEqual(second["known_threads"],1)
        self.assertEqual(second["roots"][0]["parent_thread_id"],root)

    def test_root_session_offered_when_header_was_not_indexed(self):
        root="44444444-4444-4444-8444-444444444444"
        stem="rollout-2026-11-13T10-00-00-"+root
        run_id=stable("run","codex",stem)
        self.store.add_run("Recoverable Codex root","demo",run_id)
        with self.store.lock,self.store.conn:
            self.store.conn.execute("""
                CREATE TABLE IF NOT EXISTS observer_files (
                    path TEXT PRIMARY KEY,offset INTEGER,provider TEXT,
                    run_id TEXT,last_seen TEXT
                )
            """)
            self.store.conn.execute("""
                INSERT INTO observer_files(path,offset,provider,run_id,last_seen)
                VALUES(?,0,'codex',?,'2026-11-13')
            """,(str(self.root/(stem+".jsonl")),run_id))
        result=codex_snapshot(self.store,self.store.run_list())
        candidate=next(x for x in result["roots"] if x["thread_id"]==root)
        self.assertFalse(candidate["root_confirmed"])
        self.assertEqual(candidate["run_id"],run_id)

    def test_spawn_tool_response_creates_real_child_without_rollout(self):
        root="11111111-1111-4111-8111-111111111111"
        child="22222222-2222-4222-8222-222222222222"
        path=self.rollout(root)
        with path.open("a",encoding="utf-8") as f:
            f.write(json.dumps({"type":"response_item","payload":{
                "type":"function_call","name":"spawn_agent",
                "call_id":"call-1","arguments":"{\"message\":\"private\"}"}})+"\n")
            f.write(json.dumps({"type":"response_item","payload":{
                "type":"function_call_output","call_id":"call-1",
                "output":json.dumps({"agent_id":child})}})+"\n")
        self.observer._scan_file("codex",path)
        result=codex_snapshot(self.store,self.store.run_list())
        parent=next(x for x in result["roots"] if x["thread_id"]==root)
        self.assertEqual(parent["children"][0]["thread_id"],child)
        self.assertFalse(parent["children"][0]["run_id"])
        self.assertNotIn("private",str(result))

    def test_frontend_reads_codex_roots_contract(self):
        ui=(Path(__file__).resolve().parents[1]/"monitor"/"static"/"index.html").read_text(encoding="utf-8")
        self.assertIn("latestCodexTree.roots",ui)
        self.assertIn("topology.roots",ui)
        self.assertNotIn("latestCodexTree.codexRoots",ui)
        self.assertNotIn("topology.codexRoots",ui)

    def test_no_parent_guessed_from_other_metadata(self):
        parent,source=source_parent({
            "id":"22222222-2222-4222-8222-222222222222",
            "cwd":"C:/src/demo",
            "agent_nickname":"Reviewer",
            "source":"vscode"})
        self.assertIsNone(parent)

    def test_unknown_parent_remains_unlinked(self):
        child="22222222-2222-4222-8222-222222222222"
        parent="11111111-1111-4111-8111-111111111111"
        file=self.rollout(child,{"subagent":{"thread_spawn":{
            "parent_thread_id":parent}}})
        self.observer._scan_file("codex",file)
        tree=codex_snapshot(self.store,self.store.run_list())
        self.assertEqual(tree["roots"][0]["thread_id"],child)
        self.assertEqual(tree["roots"][0]["parent_thread_id"],parent)


if __name__=="__main__":
    unittest.main()
