"""Provider-neutral workflow plans shared by Orca, Claude and Codex.

Plan phases are not tied to a single CLI session. Ownership and execution
are separate: a phase can change provider while keeping stable identity.
"""
from __future__ import annotations
from datetime import datetime, timezone
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, Field, model_validator

PROVIDERS = {"codex", "claude", "orca", "system", "other"}
STATUSES = {"pending", "running", "completed", "failed", "blocked", "cancelled", "waiting"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PhaseInput(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    parent_id: str | None = Field(default=None, max_length=100)
    provider: Literal["codex", "claude", "orca", "system", "other"] = "other"
    agent_id: str = Field(default="", max_length=128)
    status: Literal["pending", "running", "completed", "failed", "blocked", "cancelled", "waiting"] = "pending"
    order: int = Field(default=0, ge=0)


class PlanInput(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    project: str = Field(default="", max_length=200)
    phases: list[PhaseInput] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_tree(self):
        ids = {p.id for p in self.phases}
        if len(ids) != len(self.phases):
            raise ValueError("Duplicate phase ids")
        parents = {p.id: p.parent_id for p in self.phases}
        for phase in self.phases:
            if phase.parent_id and phase.parent_id not in ids:
                raise ValueError("Unknown parent phase")
            seen = {phase.id}
            current = phase.parent_id
            while current:
                if current in seen:
                    raise ValueError("Cyclic phase tree")
                seen.add(current)
                current = parents[current]
        return self


class PhaseUpdate(BaseModel):
    status: Literal["pending", "running", "completed", "failed", "blocked", "cancelled", "waiting"] | None = None
    provider: Literal["codex", "claude", "orca", "system", "other"] | None = None
    agent_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)


def initialize(store):
    with store.lock, store.conn:
        store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_plans (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflow_phases (
                plan_id TEXT NOT NULL REFERENCES workflow_plans(id),
                id TEXT NOT NULL, parent_id TEXT, title TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'other', agent_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending', phase_order INTEGER NOT NULL DEFAULT 0,
                session_id TEXT, started_at TEXT, finished_at TEXT,
                updated_at TEXT NOT NULL, PRIMARY KEY(plan_id,id),
                FOREIGN KEY(plan_id,parent_id) REFERENCES workflow_phases(plan_id,id)
                    DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TABLE IF NOT EXISTS workflow_phase_events (
                plan_id TEXT NOT NULL, event_id TEXT NOT NULL,
                phase_id TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(plan_id,event_id)
            );
            CREATE TABLE IF NOT EXISTS workflow_phase_history (
                plan_id TEXT NOT NULL, phase_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                previous_provider TEXT NOT NULL, next_provider TEXT NOT NULL,
                previous_agent_id TEXT NOT NULL, next_agent_id TEXT NOT NULL,
                previous_session_id TEXT, next_session_id TEXT,
                previous_status TEXT NOT NULL, next_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(plan_id,event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_phase_history
                ON workflow_phase_history(plan_id,phase_id,created_at);
        """)


def upsert_plan(store, data: PlanInput):
    plan_id = data.id or str(uuid4())
    timestamp = now()
    with store.lock, store.conn:
        store.conn.execute("""
            INSERT INTO workflow_plans(id,title,project,created_at,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                project=excluded.project,updated_at=excluded.updated_at
        """, (plan_id,data.title,data.project,timestamp,timestamp))
        for p in data.phases:
            store.conn.execute("""
                INSERT INTO workflow_phases(
                    plan_id,id,parent_id,title,provider,agent_id,status,phase_order,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(plan_id,id) DO UPDATE SET
                    title=excluded.title, provider=excluded.provider,
                    agent_id=excluded.agent_id,phase_order=excluded.phase_order
            """, (plan_id,p.id,p.parent_id,p.title,p.provider,p.agent_id,p.status,
                  p.order,timestamp))
        # Parent links updated after all phases exist, preserving FK integrity.
        for p in data.phases:
            store.conn.execute(
                "UPDATE workflow_phases SET parent_id=? WHERE plan_id=? AND id=?",
                (p.parent_id,plan_id,p.id)
            )
    return {"id":plan_id,"phases":len(data.phases)}


def update_phase(store, plan_id: str, phase_id: str, update: PhaseUpdate):
    timestamp=now()
    with store.lock, store.conn:
        plan=store.conn.execute(
            "SELECT 1 FROM workflow_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if not plan:
            raise KeyError("Unknown plan")
        phase=store.conn.execute(
            "SELECT * FROM workflow_phases WHERE plan_id=? AND id=?",
            (plan_id,phase_id)
        ).fetchone()
        if not phase:
            raise KeyError("Unknown phase")
        done=store.conn.execute(
            "SELECT 1 FROM workflow_phase_events WHERE plan_id=? AND event_id=?",
            (plan_id,update.event_id)
        ).fetchone()
        if done:
            return {"updated":False,"duplicate":True}
        status=update.status or phase["status"]
        provider=update.provider or phase["provider"]
        agent=update.agent_id if update.agent_id is not None else phase["agent_id"]
        session=update.session_id if update.session_id is not None else phase["session_id"]
        started=phase["started_at"] or (timestamp if status=="running" else None)
        finished=timestamp if status in ("completed","failed","cancelled") else None
        store.conn.execute("""
            UPDATE workflow_phases SET status=?,provider=?,agent_id=?,session_id=?,
                started_at=?,finished_at=?,updated_at=?
            WHERE plan_id=? AND id=?
        """,(status,provider,agent,session,started,finished,timestamp,plan_id,phase_id))
        store.conn.execute(
            "INSERT INTO workflow_phase_events VALUES(?,?,?,?)",
            (plan_id,update.event_id,phase_id,timestamp)
        )
        store.conn.execute("""
            INSERT INTO workflow_phase_history(
                plan_id,phase_id,event_id,previous_provider,next_provider,
                previous_agent_id,next_agent_id,previous_session_id,next_session_id,
                previous_status,next_status,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """, (plan_id,phase_id,update.event_id,
              phase["provider"],provider,phase["agent_id"],agent,
              phase["session_id"],session,phase["status"],status,timestamp))
        store.conn.execute(
            "UPDATE workflow_plans SET updated_at=? WHERE id=?",
            (timestamp,plan_id)
        )
    return {"updated":True,"duplicate":False,"phase_id":phase_id,"status":status}


def snapshot(store, plan_id: str):
    with store.lock:
        plan=store.conn.execute(
            "SELECT * FROM workflow_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if not plan:
            return None
        phases=[dict(p) for p in store.conn.execute(
            "SELECT * FROM workflow_phases WHERE plan_id=? ORDER BY phase_order,id",
            (plan_id,)
        )]
        history=[dict(h) for h in store.conn.execute(
            "SELECT * FROM workflow_phase_history WHERE plan_id=? ORDER BY created_at,event_id",
            (plan_id,)
        )]
        return {"plan":dict(plan),"phases":phases,"history":history}


def plans(store):
    with store.lock:
        return [dict(p) for p in store.conn.execute(
            "SELECT * FROM workflow_plans ORDER BY updated_at DESC LIMIT 100"
        )]
