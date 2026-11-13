"""Codex subagents inferred only from explicit thread lineage in rollout metadata.

A subagent is a thread, not necessarily an OS process. Source metadata
provides parent_thread_id, allowing reliable nested agent groups without
claiming that a particular PID owns a session.
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from .observer import stable

UUID = re.compile(r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$")


def thread_id(value):
    if isinstance(value,str) and UUID.fullmatch(value):
        return value.lower()
    return None


def source_parent(payload):
    if not isinstance(payload,dict):
        return None, {}
    src=payload.get("source")
    if not isinstance(src,dict):
        src=payload.get("session_source")
    source={}
    if isinstance(src,dict):
        child=src.get("subagent") or src.get("SubAgent")
        if isinstance(child,dict):
            source=child.get("thread_spawn") or child.get("threadSpawn") or {}
    if not isinstance(source,dict):
        source={}
    parent=thread_id(source.get("parent_thread_id") or source.get("parentThreadId")
                     or payload.get("parent_thread_id"))
    return parent,source


def initialize(store):
    if getattr(store,"_codex_agents_ready",False):
        return
    with store.lock,store.conn:
        if getattr(store,"_codex_agents_ready",False):
            return
        store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS codex_agent_threads (
                thread_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                parent_thread_id TEXT,
                nickname TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                task TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_codex_agent_parent
                ON codex_agent_threads(parent_thread_id);
            CREATE TABLE IF NOT EXISTS codex_focus_sessions (
                id INTEGER PRIMARY KEY CHECK(id=1),
                pid INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                run_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS codex_agent_activity (
                thread_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS codex_spawn_calls (
                run_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                parent_thread_id TEXT NOT NULL,
                PRIMARY KEY(run_id,call_id)
            );
        """)
        store._codex_agents_ready=True


def save_thread_metadata(store, session_key, payload):
    from .db import utc
    identity=thread_id(payload.get("id"))
    if not identity:
        return None
    parent,source=source_parent(payload)
    nickname=payload.get("agent_nickname") or source.get("agent_nickname") or ""
    role=payload.get("agent_role") or source.get("agent_role") or ""
    task=source.get("task_name") or payload.get("task_name") or ""
    if not isinstance(nickname,str): nickname=""
    if not isinstance(role,str): role=""
    if not isinstance(task,str): task=""
    run_id=stable("run","codex",session_key)
    initialize(store)
    with store.lock,store.conn:
        previous=store.conn.execute("""
            SELECT run_id,parent_thread_id,nickname,role,task FROM codex_agent_threads
            WHERE thread_id=?
        """,(identity,)).fetchone()
        if previous and previous["run_id"]==run_id and \
            (previous["parent_thread_id"]==parent or parent is None) \
            and (not nickname or previous["nickname"]==nickname[:80]) \
            and (not role or previous["role"]==role[:80]) \
            and (not task or previous["task"]==task[:160]):
            return {"thread_id":identity,"parent_thread_id":previous["parent_thread_id"]}
        store.conn.execute("""
            INSERT INTO codex_agent_threads(thread_id,run_id,parent_thread_id,
                nickname,role,task,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(thread_id) DO UPDATE SET
                run_id=excluded.run_id,
                parent_thread_id=COALESCE(excluded.parent_thread_id,
                                         codex_agent_threads.parent_thread_id),
                nickname=CASE WHEN excluded.nickname!='' THEN excluded.nickname
                         ELSE codex_agent_threads.nickname END,
                role=CASE WHEN excluded.role!='' THEN excluded.role
                     ELSE codex_agent_threads.role END,
                task=CASE WHEN excluded.task!='' THEN excluded.task
                     ELSE codex_agent_threads.task END,
                updated_at=excluded.updated_at
        """,(identity,run_id,parent,nickname[:80],role[:80],task[:160],utc()))
    return {"thread_id":identity,"parent_thread_id":parent}


def save_subagent_activity(store, parent_run_id, payload):
    """Associate a child only when Codex provides its exact thread ID."""
    from .db import utc
    if not isinstance(payload,dict):
        return None
    item=payload.get("item") if isinstance(payload.get("item"),dict) else payload
    if item.get("type") not in (None,"SubAgentActivity"):
        return None
    child=thread_id(item.get("agent_thread_id"))
    kind=item.get("kind")
    if not child or kind not in ("started","interacted","interrupted","completed"):
        return None
    initialize(store)
    with store.lock,store.conn:
        parent=store.conn.execute(
            "SELECT thread_id FROM codex_agent_threads WHERE run_id=?",
            (parent_run_id,)).fetchone()
        if not parent or parent["thread_id"]==child:
            return None
        path=item.get("agent_path")
        nickname=path.rsplit("/",1)[-1][:80] if isinstance(path,str) else ""
        store.conn.execute("""
            INSERT OR IGNORE INTO codex_agent_threads(
                thread_id,run_id,parent_thread_id,nickname,role,task,updated_at
            ) VALUES(?,?,?,?,?,?,?)
        """,(child,"",parent["thread_id"],nickname,"","",utc()))
        store.conn.execute("""
            INSERT INTO codex_agent_activity(thread_id,kind,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(thread_id) DO UPDATE SET kind=excluded.kind,
                updated_at=excluded.updated_at
        """,(child,kind,utc()))
    return child



def record_spawn_call(store, parent_run_id, call_id):
    """Remember only the ID of a spawn_agent invocation, never arguments."""
    if not isinstance(call_id,str) or not call_id:
        return
    initialize(store)
    with store.lock,store.conn:
        parent=store.conn.execute(
            "SELECT thread_id FROM codex_agent_threads WHERE run_id=?",
            (parent_run_id,)).fetchone()
        if parent:
            store.conn.execute("""
                INSERT OR IGNORE INTO codex_spawn_calls(
                    run_id,call_id,parent_thread_id) VALUES(?,?,?)
            """,(parent_run_id,call_id,parent["thread_id"]))


def record_spawn_response(store, parent_run_id, call_id, output):
    """Accept an agent ID only from successfully parsed structured JSON."""
    if not isinstance(call_id,str) or not isinstance(output,(str,dict)):
        return None
    if isinstance(output,str):
        try: data=json.loads(output)
        except (ValueError,TypeError): return None
    else:
        data=output
    if not isinstance(data,dict):
        return None
    child=thread_id(data.get("agent_id") or data.get("agentId"))
    if not child:
        return None
    initialize(store)
    from .db import utc
    with store.lock,store.conn:
        call=store.conn.execute("""
            SELECT parent_thread_id FROM codex_spawn_calls
            WHERE run_id=? AND call_id=?
        """,(parent_run_id,call_id)).fetchone()
        if not call or child==call["parent_thread_id"]:
            return None
        store.conn.execute("""
            INSERT INTO codex_agent_threads(
                thread_id,run_id,parent_thread_id,nickname,role,task,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(thread_id) DO UPDATE SET
                parent_thread_id=COALESCE(codex_agent_threads.parent_thread_id,
                                          excluded.parent_thread_id)
        """,(child,"",call["parent_thread_id"],"","","",utc()))
    return child


def recover_known_rollouts(store, limit=500):
    """Recover Codex root metadata from already indexed rollout files.

    Runs once per Store instance, not for every dashboard request. Only the
    first metadata record is read, never transcript messages/tool output.
    """
    if getattr(store,"_codex_rollouts_recovered",False):
        return
    initialize(store)
    with store.lock:
        paths=[row["path"] for row in store.conn.execute("""
            SELECT path FROM observer_files WHERE provider='codex'
            ORDER BY last_seen DESC LIMIT ?
        """,(limit,))]
    for path in paths:
        try:
            with Path(path).open("rb") as handle:
                first=handle.readline(1024*1024)
            row=json.loads(first)
            if not isinstance(row,dict) or row.get("type")!="session_meta":
                continue
            payload=row.get("payload")
            if isinstance(payload,dict):
                save_thread_metadata(store,Path(path).stem,payload)
        except (OSError,ValueError,TypeError):
            continue
    store._codex_rollouts_recovered=True


def snapshot(store, runs):
    """Return roots and nested children, including previously observed child runs."""
    initialize(store)
    recover_known_rollouts(store)
    by_run={r["id"]:r for r in runs}
    with store.lock:
        items=[dict(r) for r in store.conn.execute("""
            SELECT t.*,a.kind AS activity
            FROM codex_agent_threads t
            LEFT JOIN codex_agent_activity a ON a.thread_id=t.thread_id
            ORDER BY t.thread_id
        """)]
    # The CLI may have usable rollout files even when their headers were
    # not indexed (e.g. an older observer database). Offer them explicitly
    # as *unverified root candidates*, not as proved parent threads.
    with store.lock:
        rows=store.conn.execute("""
            SELECT f.path,f.run_id,r.title,r.updated_at
            FROM observer_files f JOIN runs r ON r.id=f.run_id
            WHERE f.provider='codex' ORDER BY r.updated_at DESC LIMIT 200
        """).fetchall()
    indexed_ids={a["thread_id"] for a in items}
    for entry in rows:
        # Rollout filenames end in the canonical UUID, regardless of
        # the date-time prefix. Accept no other generated thread identity.
        match=re.search(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\\.jsonl$",entry["path"])
        if not match:
            continue
        identity=thread_id(match.group(1))
        if not identity or identity in indexed_ids:
            continue
        items.append({
            "thread_id":identity,"run_id":entry["run_id"],
            "parent_thread_id":None,"nickname":"","role":"",
            "task":"","updated_at":entry["updated_at"],
            "activity":None,"root_confirmed":False,
        })
        indexed_ids.add(identity)
    for item in items:
        item.setdefault("root_confirmed",True)
    indexed={a["thread_id"]:a for a in items}
    grouped={}
    for agent in items:
        run=by_run.get(agent["run_id"])
        agent["title"]=(agent["task"] or agent["nickname"] or agent["role"]
                         or (run["title"] if run else "Codex agent"))
        agent["run"]=run
        agent["children"]=[]
    for agent in items:
        parent=indexed.get(agent["parent_thread_id"])
        if parent and parent is not agent:
            # Guard against invalid cyclic metadata.
            cursor=parent
            seen={agent["thread_id"]}
            valid=True
            while cursor and cursor["thread_id"] not in seen:
                seen.add(cursor["thread_id"])
                cursor=indexed.get(cursor["parent_thread_id"])
            if cursor and cursor["thread_id"] in seen:
                valid=False
            if valid:
                parent["children"].append(agent)
                continue
        grouped[agent["thread_id"]]=agent
    return {"roots":list(grouped.values()),
            "known_threads":len(items),
            "subagents":sum(bool(a["parent_thread_id"]) for a in items)}


def collect_descendants(tree):
    """Flatten thread subagent hierarchy, excluding its root."""
    result=[]
    def walk(agent):
        for child in agent.get("children",[]):
            result.append(child)
            walk(child)
    walk(tree)
    return result
