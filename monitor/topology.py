"""Build a process tree from verified OS ancestry.

The tree represents process ancestry, NOT an asserted agent delegation graph.
Never associate independent processes by title, project, or creation time.
"""
from __future__ import annotations

def build_process_tree(processes, relationships, runs):
    by_pid = {}
    for p in processes:
        pid = p.get("pid")
        if not isinstance(pid, int) or pid in by_pid:
            continue
        by_pid[pid] = {
            "pid": pid,
            "ppid": p.get("ppid"),
            "provider": p.get("provider"),
            "session_id": p.get("session_id"),
            "started_at": p.get("started_at"),
            "session": None,
            "relation": None,
            "children": []
        }

    for run in runs:
        for pid in run.get("pids") or []:
            if pid in by_pid:
                by_pid[pid]["session"] = {
                    "id": run["id"], "title": run["title"],
                    "project": run.get("project", ""),
                    "presence": run.get("presence", "inactive"),
                    "activity": run.get("activity")
                }

    # If a process is both a parent and child, use the closest observed
    # ancestor only, with deterministic precedence for duplicate records.
    candidates = sorted(relationships, key=lambda r: (r.get("depth", 999), r.get("child_pid", 0)))
    attached = set()
    for link in candidates:
        parent = link.get("parent_pid")
        child = link.get("child_pid")
        if parent not in by_pid or child not in by_pid or parent == child or child in attached:
            continue
        # Reject cycles, including indirectly.
        cursor = parent
        seen = set()
        cyclic = False
        while cursor in by_pid and cursor not in seen:
            if cursor == child:
                cyclic = True
                break
            seen.add(cursor)
            cursor = by_pid[cursor].get("_parent")
        if cyclic:
            continue
        item = by_pid[child]
        item["relation"] = link.get("relation") or "process_ancestor"
        item["_parent"] = parent
        by_pid[parent]["children"].append(item)
        attached.add(child)

    def clean(node):
        node.pop("_parent", None)
        node["children"].sort(key=lambda c: (c["provider"] != "codex", c["pid"]))
        for child in node["children"]:
            clean(child)

    roots = [p for pid,p in by_pid.items() if pid not in attached]
    roots.sort(key=lambda n: (n["provider"] != "codex", n["pid"]))
    for root in roots:
        clean(root)
    return {
        "roots": roots,
        "process_count": len(by_pid),
        "root_count": len(roots),
        "linked_sessions": sum(p["session"] is not None for p in by_pid.values())
    }
