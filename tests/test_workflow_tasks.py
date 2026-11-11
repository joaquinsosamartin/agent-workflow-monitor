"""Claude task synchronisation into explicit shared plans (offline tests)."""
import tempfile
import unittest
from pathlib import Path
from monitor.db import Store
from monitor.workflow_plan import initialize, PlanInput, upsert_plan, snapshot
from monitor.workflow_tasks import ClaudeTaskEvent, import_task, task_phase_id


class ClaudeTaskImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/"monitor.db")
        initialize(self.store)
        upsert_plan(self.store,PlanInput(id="shared",title="Shared delivery",phases=[
            {"id":"backend","title":"Implement backend","provider":"codex","agent_id":"codex-dev"}
        ]))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_create_then_update_without_touching_codex(self):
        event=ClaudeTaskEvent(plan_id="shared",session_id="claude-s1",
            task_id="42",action="created",subject="Review API security",
            status="pending",event_id="c1")
        first=import_task(self.store,event)
        self.assertTrue(first["updated"])
        self.assertEqual(import_task(self.store,event)["duplicate"],True)
        update=ClaudeTaskEvent(plan_id="shared",session_id="claude-s1",
            task_id="42",action="updated",status="in_progress",
            owner="claude-qa",event_id="u1")
        self.assertEqual(import_task(self.store,update)["status"],"running")
        plan=snapshot(self.store,"shared")
        codex=next(x for x in plan["phases"] if x["id"]=="backend")
        claude=next(x for x in plan["phases"] if x["id"]==task_phase_id("claude-s1","42"))
        self.assertEqual(codex["provider"],"codex")
        self.assertEqual(codex["status"],"pending")
        self.assertEqual(claude["provider"],"claude")
        self.assertEqual(claude["agent_id"],"claude-qa")
        self.assertEqual(claude["session_id"],"claude-s1")
        self.assertEqual(len(plan["history"]),2)

    def test_unknown_task_update_rejected(self):
        with self.assertRaises(KeyError):
            import_task(self.store,ClaudeTaskEvent(plan_id="shared",
                session_id="claude-s1",task_id="unknown",action="updated",
                event_id="update-only",status="completed"))

    def test_missing_plan_rejected(self):
        with self.assertRaises(KeyError):
            import_task(self.store,ClaudeTaskEvent(plan_id="missing",
                session_id="claude-s1",task_id="42",action="created",
                subject="Task",event_id="c1"))

    def test_task_ids_are_scoped_by_session(self):
        self.assertNotEqual(task_phase_id("claude-1","42"),
                            task_phase_id("claude-2","42"))


if __name__=="__main__":
    unittest.main()
