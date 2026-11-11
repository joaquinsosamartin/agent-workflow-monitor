"""Offline tests for the process tree (no Windows services or GitHub Actions)."""
import unittest
from monitor.topology import build_process_tree


class ProcessTreeTests(unittest.TestCase):
    def test_codex_and_child_claudes(self):
        processes = [
            {"pid": 100, "ppid": 1, "provider": "codex", "started_at": "2026-11-11T09:00:00Z"},
            {"pid": 200, "ppid": 100, "provider": "claude", "started_at": "2026-11-11T09:01:00Z"},
            {"pid": 300, "ppid": 100, "provider": "claude", "started_at": "2026-11-11T09:02:00Z"}
        ]
        links = [
            {"parent_pid": 100, "child_pid": 200, "relation": "process_parent", "depth": 1},
            {"parent_pid": 100, "child_pid": 300, "relation": "process_parent", "depth": 1}
        ]
        runs=[{"id":"r1","title":"Implement customers","presence":"active","pids":[100]}]
        result=build_process_tree(processes,links,runs)
        self.assertEqual(result["root_count"],1)
        self.assertEqual(result["process_count"],3)
        self.assertEqual(result["roots"][0]["provider"],"codex")
        self.assertEqual({n["pid"] for n in result["roots"][0]["children"]},{200,300})
        self.assertEqual(result["roots"][0]["session"]["id"],"r1")

    def test_unverified_claude_remains_separate(self):
        processes=[{"pid":10,"provider":"codex"},{"pid":20,"provider":"claude"}]
        result=build_process_tree(processes,[],[])
        self.assertEqual([p["pid"] for p in result["roots"]],[10,20])
        self.assertEqual(result["linked_sessions"],0)

    def test_user_confirmed_link_overrides_process_ancestry(self):
        processes=[
            {"pid":10,"provider":"codex"},
            {"pid":20,"provider":"claude"},
            {"pid":30,"provider":"claude"}
        ]
        observed=[{"parent_pid":30,"child_pid":20,"relation":"process_parent","depth":1}]
        confirmed=[{"parent_pid":10,"child_pid":20}]
        result=build_process_tree(processes,observed,[],confirmed)
        codex=next(n for n in result["roots"] if n["pid"]==10)
        self.assertEqual(len(codex["children"]),1)
        self.assertEqual(codex["children"][0]["pid"],20)
        self.assertEqual(codex["children"][0]["relation"],"user_confirmed")

    def test_cycles_are_ignored(self):
        processes=[{"pid":10,"provider":"codex"},{"pid":20,"provider":"claude"}]
        links=[
            {"parent_pid":10,"child_pid":20,"depth":1},
            {"parent_pid":20,"child_pid":10,"depth":1}
        ]
        tree=build_process_tree(processes,links,[])
        self.assertEqual(tree["process_count"],2)
        self.assertEqual(tree["root_count"],1)


if __name__=="__main__":
    unittest.main()
