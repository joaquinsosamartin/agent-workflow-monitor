"""Local Claude Mod bridge contract tests: no network, secrets or prompts."""
import tempfile
import unittest
from pathlib import Path

from monitor.db import Store
from monitor.mod_bridge import ModEvent, ingest
from monitor.observer import stable


class ModBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/"test.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_mod_events_are_idempotent_and_reuse_transcript_run(self):
        sid = "12345678-1111-2222-3333-123456789abc"
        event=ModEvent(
            session_id=sid,cwd=r"C:\git\customer-api",
            status="running",event_id="event-001",
            agents=[{"id":"qa","title":"Tests","status":"running","model":"claude"}],
            tool="PowerShell",agent_id="qa",context_tokens=50000,context_window=1000000)
        first=ingest(self.store,event)
        ingest(self.store,event)
        rid=stable("run","claude",sid)
        self.assertEqual(first["run_id"],rid)
        snap=self.store.snapshot(rid)
        self.assertEqual(snap["run"]["project"],"customer-api")
        self.assertEqual(len(snap["nodes"]),2)
        self.assertEqual(snap["usage"],[])
        self.assertEqual(len(self.store.event_list(rid)),3)
        contents=str(self.store.event_list(rid))
        self.assertIn("PowerShell",contents)
        self.assertNotIn("50000",contents)

    def test_existing_transcript_run_is_reused(self):
        sid="12345678-1111-2222-3333-123456789abc"
        rid=stable("run","claude",sid)
        self.store.add_run("My existing session","existing-project",rid)
        result=ingest(self.store,ModEvent(
            session_id=sid,status="waiting",event_id="wait-001"))
        self.assertEqual(result["run_id"],rid)
        self.assertEqual(len(self.store.run_list()),1)
        self.assertEqual(self.store.snapshot(rid)["run"]["title"],"My existing session")

    def test_duplicate_status_events_are_skipped(self):
        sid="12345678-1111-2222-3333-123456789abc"
        base=dict(session_id=sid,status="running",agents=[
            {"id":"dev","title":"Backend","status":"running"}])
        ingest(self.store,ModEvent(**base,event_id="tick-1"))
        rid=stable("run","claude",sid)
        count=len(self.store.event_list(rid))
        ingest(self.store,ModEvent(**base,event_id="tick-2"))
        self.assertEqual(len(self.store.event_list(rid)),count)


if __name__ == "__main__":
    unittest.main()
