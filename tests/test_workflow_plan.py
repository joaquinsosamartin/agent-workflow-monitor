"""Provider-neutral workflow plan tests, no remote dependencies."""
import tempfile
import unittest
from pathlib import Path
from pydantic import ValidationError
from monitor.db import Store
from monitor.workflow_plan import (
    PlanInput, PhaseUpdate, initialize, upsert_plan, update_phase, snapshot
)


class WorkflowPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/"test.sqlite")
        initialize(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_codex_to_claude_handoff_preserves_phase(self):
        data=PlanInput(id="pops-plan",title="Customer API",project="POPS",phases=[
            {"id":"implementation","title":"Implement backend","provider":"codex","agent_id":"backend","order":0},
            {"id":"review","parent_id":"implementation","title":"Review","provider":"claude",
             "agent_id":"reviewer","order":1}
        ])
        upsert_plan(self.store,data)
        update_phase(self.store,"pops-plan","implementation",PhaseUpdate(
            status="running",provider="codex",agent_id="backend",session_id="codex-1",event_id="ev1"))
        update_phase(self.store,"pops-plan","implementation",PhaseUpdate(
            status="running",provider="claude",agent_id="reviewer",session_id="claude-1",event_id="ev2"))
        result=snapshot(self.store,"pops-plan")
        main=next(p for p in result["phases"] if p["id"]=="implementation")
        self.assertEqual(main["provider"],"claude")
        self.assertEqual(main["agent_id"],"reviewer")
        self.assertEqual(main["session_id"],"claude-1")
        self.assertEqual(main["status"],"running")
        self.assertEqual(len(result["phases"]),2)

    def test_reimport_does_not_undo_provider_handoff(self):
        original=PlanInput(id="reimport",title="Plan",phases=[
            {"id":"phase","title":"Implementación","provider":"codex","agent_id":"dev"}
        ])
        upsert_plan(self.store,original)
        update_phase(self.store,"reimport","phase",PhaseUpdate(
            provider="claude",agent_id="reviewer",session_id="c-session",
            status="running",event_id="handoff"))
        upsert_plan(self.store,original)
        result=snapshot(self.store,"reimport")
        self.assertEqual(result["phases"][0]["provider"],"claude")
        self.assertEqual(result["phases"][0]["agent_id"],"reviewer")
        self.assertEqual(result["phases"][0]["session_id"],"c-session")
        self.assertEqual(len(result["history"]),1)
        self.assertEqual(result["history"][0]["previous_provider"],"codex")
        self.assertEqual(result["history"][0]["next_provider"],"claude")

    def test_idempotent_phase_event(self):
        upsert_plan(self.store,PlanInput(id="plan",title="Test",phases=[
            {"id":"phase","title":"Step","provider":"codex"}]))
        event=PhaseUpdate(status="completed",provider="claude",event_id="a1")
        self.assertTrue(update_phase(self.store,"plan","phase",event)["updated"])
        self.assertTrue(update_phase(self.store,"plan","phase",event)["duplicate"])
        self.assertEqual(snapshot(self.store,"plan")["phases"][0]["status"],"completed")

    def test_reject_cycles(self):
        with self.assertRaises(ValidationError):
            PlanInput(title="Cyclic",phases=[
                {"id":"a","title":"A","parent_id":"b"},
                {"id":"b","title":"B","parent_id":"a"}
            ])

    def test_plan_upsert_preserves_work(self):
        data=PlanInput(id="p",title="Title",phases=[
            {"id":"a","title":"A","provider":"codex"}])
        upsert_plan(self.store,data)
        update_phase(self.store,"p","a",PhaseUpdate(status="running",event_id="x"))
        upsert_plan(self.store,data)
        self.assertEqual(snapshot(self.store,"p")["phases"][0]["status"],"running")


if __name__=="__main__":
    unittest.main()
