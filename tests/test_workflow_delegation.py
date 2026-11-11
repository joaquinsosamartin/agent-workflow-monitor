"""Local delegation tests: no subprocesses, network calls, or GitHub Actions."""
import tempfile
import unittest
from pathlib import Path

from monitor.db import Store
from monitor.workflow_plan import (
    PlanInput, PhaseUpdate, initialize as init_plans,
    upsert_plan, update_phase, snapshot as workflow_snapshot,
)
from monitor.workflow_delegation import (
    DelegationStart, DelegationFinish, start, finish, list_for_plan,
)


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/"local.db")
        init_plans(self.store)
        upsert_plan(self.store,PlanInput(id="p1",title="API",phases=[
            {"id":"implementation","title":"Backend","provider":"codex","agent_id":"orchestrator"},
            {"id":"qa","title":"Review","provider":"claude","agent_id":"reviewer"},
        ]))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_codex_delegates_to_claude(self):
        payload=DelegationStart(
            execution_id="exec-1",provider="claude",agent_id="qa-agent",
            session_id="claude-session",
            orchestrator_provider="codex",orchestrator_session_id="codex-session",
            pid=5320,
        )
        first=start(self.store,"p1","qa",payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(start(self.store,"p1","qa",payload)["duplicate"])
        running=workflow_snapshot(self.store,"p1")
        qa=next(x for x in running["phases"] if x["id"]=="qa")
        self.assertEqual(qa["status"],"running")
        self.assertEqual(qa["session_id"],"claude-session")
        history=list_for_plan(self.store,"p1")
        self.assertEqual(len(history),1)
        self.assertEqual(history[0]["orchestrator_provider"],"codex")
        self.assertEqual(history[0]["orchestrator_session_id"],"codex-session")
        finished=finish(self.store,"exec-1",DelegationFinish(exit_code=0,event_id="end-1"))
        self.assertTrue(finished["phase_updated"])
        self.assertEqual(finished["status"],"completed")
        self.assertTrue(finish(self.store,"exec-1",DelegationFinish(exit_code=0,event_id="end-1"))["duplicate"])
        state=workflow_snapshot(self.store,"p1")
        self.assertEqual(next(x for x in state["phases"] if x["id"]=="qa")["status"],"completed")
        self.assertEqual(next(x for x in state["phases"] if x["id"]=="implementation")["provider"],"codex")

    def test_late_completion_cannot_overwrite_reassigned_phase(self):
        start(self.store,"p1","implementation",DelegationStart(
            execution_id="old",provider="claude",agent_id="first",session_id="s1",
            orchestrator_provider="codex"))
        update_phase(self.store,"p1","implementation",PhaseUpdate(
            provider="codex",agent_id="second",session_id="s2",
            status="running",event_id="handoff"))
        result=finish(self.store,"old",DelegationFinish(exit_code=0,event_id="end"))
        self.assertFalse(result["phase_updated"])
        phase=next(x for x in workflow_snapshot(self.store,"p1")["phases"] if x["id"]=="implementation")
        self.assertEqual(phase["status"],"running")
        self.assertEqual(phase["agent_id"],"second")

    def test_rejects_execution_id_reuse_in_different_phase(self):
        start(self.store,"p1","qa",DelegationStart(
            execution_id="same",provider="claude",agent_id="agent"))
        with self.assertRaises(ValueError):
            start(self.store,"p1","implementation",DelegationStart(
                execution_id="same",provider="claude",agent_id="agent"))


if __name__=="__main__":
    unittest.main()
