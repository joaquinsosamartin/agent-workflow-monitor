"""Import confirmed Claude TaskCreate/TaskUpdate results into a selected shared plan.

Never infer task ownership from proximity, prompt text or tool names alone.
Task IDs are scoped to the Claude session and mapped to persistent phase IDs.
"""
from __future__ import annotations

import hashlib
from typing import Literal
from pydantic import BaseModel, Field
from .workflow_plan import now


class ClaudeTaskEvent(BaseModel):
    plan_id: str = Field(min_length=1,max_length=128)
    session_id: str = Field(min_length=1,max_length=128)
    task_id: str = Field(min_length=1,max_length=128)
    action: Literal["created","updated"]
    subject: str | None = Field(default=None,max_length=200)
    status: Literal["pending","in_progress","completed","deleted"] | None = None
    owner: str | None = Field(default=None,max_length=128)
    event_id: str = Field(min_length=1,max_length=128)


def task_phase_id(session_id: str, task_id: str) -> str:
    digest=hashlib.sha256(f"{session_id}\0{task_id}".encode()).hexdigest()[:32]
    return f"claude-task-{digest}"


def import_task(store, event: ClaudeTaskEvent):
    phase_id=task_phase_id(event.session_id,event.task_id)
    status={
        "in_progress":"running","deleted":"cancelled"
    }.get(event.status,event.status)
    stamp=now()
    with store.lock,store.conn:
        plan=store.conn.execute(
            "SELECT 1 FROM workflow_plans WHERE id=?", (event.plan_id,)
        ).fetchone()
        if not plan:
            raise KeyError("Plan not found")
        existing=store.conn.execute(
            "SELECT * FROM workflow_phases WHERE plan_id=? AND id=?",
            (event.plan_id,phase_id)
        ).fetchone()
        if existing is None and event.action!="created":
            raise KeyError("TaskCreate must be observed before TaskUpdate")
        if existing and existing["provider"]!="claude":
            raise ValueError("Cannot overwrite a non-Claude phase")
        already=store.conn.execute(
            "SELECT 1 FROM workflow_phase_events WHERE plan_id=? AND event_id=?",
            (event.plan_id,event.event_id)
        ).fetchone()
        if already:
            return {"updated":False,"duplicate":True,"phase_id":phase_id}
        if existing is None:
            if not event.subject:
                raise ValueError("Task subject required on creation")
            store.conn.execute("""
                INSERT INTO workflow_phases(
                    plan_id,id,parent_id,title,provider,agent_id,status,phase_order,
                    session_id,started_at,finished_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,(event.plan_id,phase_id,None,event.subject,"claude",
                 event.owner or "",status or "pending",1000,
                 event.session_id,stamp if status=="running" else None,
                 stamp if status in ("completed","cancelled") else None,stamp))
            old={"provider":"claude","agent_id":"","session_id":None,"status":"pending"}
        else:
            old=dict(existing)
            new_status=status or old["status"]
            store.conn.execute("""
                UPDATE workflow_phases SET
                    title=?,agent_id=?,status=?,session_id=?,started_at=?,
                    finished_at=?,updated_at=?
                WHERE plan_id=? AND id=?
            """, (event.subject if event.subject is not None else old["title"],
                  event.owner if event.owner is not None else old["agent_id"],
                  new_status,event.session_id,
                  old["started_at"] or (stamp if new_status=="running" else None),
                  stamp if new_status in ("completed","cancelled") else None,
                  stamp,event.plan_id,phase_id))
        after=store.conn.execute(
            "SELECT * FROM workflow_phases WHERE plan_id=? AND id=?",
            (event.plan_id,phase_id)).fetchone()
        store.conn.execute("INSERT INTO workflow_phase_events VALUES(?,?,?,?)",
                           (event.plan_id,event.event_id,phase_id,stamp))
        store.conn.execute("""
            INSERT INTO workflow_phase_history(
                plan_id,phase_id,event_id,previous_provider,next_provider,
                previous_agent_id,next_agent_id,previous_session_id,next_session_id,
                previous_status,next_status,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,(event.plan_id,phase_id,event.event_id,
             old["provider"],after["provider"],old["agent_id"],after["agent_id"],
             old["session_id"],after["session_id"],old["status"],after["status"],stamp))
        store.conn.execute("UPDATE workflow_plans SET updated_at=? WHERE id=?",
                           (stamp,event.plan_id))
    return {"updated":True,"duplicate":False,"phase_id":phase_id,"status":after["status"]}
