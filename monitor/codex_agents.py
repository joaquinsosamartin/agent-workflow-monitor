"""Codex subagents inferred only from explicit thread lineage in rollout metadata.

A subagent is a thread, not necessarily an OS process. Source metadata
provides parent_thread_id, allowing reliable nested agent groups without
claiming that a particular PID owns a session.
"""
from __future__ import annotations

import re
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
    with store.lock,store.conn:
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
        """)


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
            SELECT parent_thread_id,nickname,role,task FROM codex_agent_threads
            WHERE thread_id=?
        """,(identity,)).fetchone()
        if previous and (previous["parent_thread_id"]==parent or parent is None) \
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


def snapshot(store, runs):
    """Return roots and nested children, including previously observed child runs."""
    initialize(store)
    by_run={r["id"]:r for r in runs}
    with store.lock:
        items=[dict(r) for r in store.conn.execute(
            "SELECT * FROM codex_agent_threads ORDER BY thread_id")]
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
