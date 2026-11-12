"""Focused orchestrator tests: explicit identity and safe bulk links."""
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

from monitor.db import Store


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.module=importlib.import_module("monitor.app")
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/"monitor.db")
        with self.store.lock,self.store.conn:
            self.store.conn.execute("""
                CREATE TABLE monitor_focus (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    pid INTEGER NOT NULL,started_at TEXT NOT NULL)
            """)
            self.store.conn.execute("""
                CREATE TABLE process_links (
                    child_pid INTEGER NOT NULL,child_started_at TEXT NOT NULL,
                    parent_pid INTEGER NOT NULL,parent_started_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(child_pid,child_started_at))
            """)
        self.processes=[
            {"pid":10,"provider":"codex","started_at":"t10"},
            {"pid":11,"provider":"codex","started_at":"t11"},
            {"pid":20,"provider":"claude","started_at":"t20"},
            {"pid":21,"provider":"claude","started_at":"t21"},
        ]
        self.store_patch=patch.object(self.module,"store",self.store)
        self.snapshot_patch=patch.object(
            self.module.process_registry,"snapshot",
            side_effect=lambda:{"processes":self.processes})
        self.store_patch.start();self.snapshot_patch.start()

    def tearDown(self):
        self.snapshot_patch.stop();self.store_patch.stop()
        self.store.close();self.tmp.cleanup()

    def test_explicit_focus_survives_page_refresh(self):
        self.module._set_focus(self.module.FocusIn(pid=11))
        self.assertEqual(self.module.current_focus({"processes":self.processes})["pid"],11)
        self.assertIsNone(self.module.current_focus({"processes":[
            {"pid":11,"provider":"codex","started_at":"reused-PID"}
        ]}))

    def test_cannot_select_claude_as_orchestrator(self):
        with self.assertRaises(HTTPException):
            self.module._set_focus(self.module.FocusIn(pid=20))

    def test_bulk_link_preserves_creation_time_and_is_idempotent(self):
        data=self.module.LinkManyIn(parent_pid=11,child_pids=[20,21,20])
        self.assertEqual(self.module._link_many(data)["linked"],2)
        self.module._link_many(data)
        links=self.module.current_manual_links({"processes":self.processes})
        self.assertEqual({(p["parent_pid"],p["child_pid"]) for p in links},
                         {(11,20),(11,21)})
        changed=[dict(p) for p in self.processes]
        changed[-1]["started_at"]="pid-reused"
        self.assertEqual(len(self.module.current_manual_links({"processes":changed})),1)

    def test_bulk_link_rejects_invalid_child_without_partial_changes(self):
        with self.assertRaises(HTTPException):
            self.module._link_many(self.module.LinkManyIn(parent_pid=11,child_pids=[20,999]))
        self.assertEqual(self.module.current_manual_links({"processes":self.processes}),[])


if __name__=="__main__":
    unittest.main()
