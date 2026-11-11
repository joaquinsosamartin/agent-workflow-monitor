"""Explicit, provider-neutral execution records for delegated workflow phases.

A record is created only by a caller that actually launches a process (or
an equivalent trusted agent adapter). We never infer delegation from PID
ancestry, tool names or timestamps. Commands/prompts are never persisted.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field

from .workflow_plan import PhaseUpdate, update_phase

Provider = Literal["codex", "claude", "orca", "system", "other"]


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DelegationStart(BaseModel):
    execution_id: str = Field(min_length=1, max_length=128)
    provider: Provider
    agent_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    orchestrator_provider: Provider | None = None
    orchestrator_session_id: str | None = Field(default=None, max_length=128)
    pid: int | None = Field(default=None, gt=0)


class DelegationFinish(BaseModel):
    exit_code: int
    event_id: str = Field(min_length=1, max_length=128)


def initialize(store) -> None:
    with store.lock, store.conn:
        store.conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_delegations (
                execution_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                phase_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT,
                orchestrator_provider TEXT,
                orchestrator_session_id TEXT,
                pid INTEGER,
                status TEXT NOT NULL,
                exit_code INTEGER,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY(plan_id,phase_id)
                    REFERENCES workflow_phases(plan_id,id)
            )
        """)
        store.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_delegations_plan
            ON workflow_delegations(plan_id,phase_id,started_at)
        """)


def start(store, plan_id: str, phase_id: str, data: DelegationStart) -> dict:
    initialize(store)
    with store.lock, store.conn:
        existing = store.conn.execute(
            "SELECT * FROM workflow_delegations WHERE execution_id=?",
            (data.execution_id,),
        ).fetchone()
        if existing:
            if existing["plan_id"] != plan_id or existing["phase_id"] != phase_id:
                raise ValueError("Execution ID belongs to another phase")
            return {"execution_id": data.execution_id, "duplicate": True}
        phase = store.conn.execute(
            "SELECT 1 FROM workflow_phases WHERE plan_id=? AND id=?",
            (plan_id, phase_id),
        ).fetchone()
        if not phase:
            raise KeyError("Unknown workflow phase")
        update_phase(
            store, plan_id, phase_id,
            PhaseUpdate(
                provider=data.provider,
                agent_id=data.agent_id,
                session_id=data.session_id,
                status="running",
                event_id="delegation:start:" + data.execution_id,
            ),
        )
        store.conn.execute("""
            INSERT INTO workflow_delegations (
                execution_id,plan_id,phase_id,provider,agent_id,session_id,
                orchestrator_provider,orchestrator_session_id,pid,status,started_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.execution_id, plan_id, phase_id, data.provider,
            data.agent_id, data.session_id, data.orchestrator_provider,
            data.orchestrator_session_id, data.pid, "running", utc(),
        ))
    return {"execution_id": data.execution_id, "duplicate": False}


def finish(store, execution_id: str, data: DelegationFinish) -> dict:
    initialize(store)
    with store.lock, store.conn:
        execution = store.conn.execute(
            "SELECT * FROM workflow_delegations WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if not execution:
            raise KeyError("Unknown execution")
        if execution["finished_at"] is not None:
            return {
                "execution_id": execution_id, "duplicate": True,
                "status": execution["status"],
            }
        status = "completed" if data.exit_code == 0 else "failed"
        timestamp = utc()
        store.conn.execute("""
            UPDATE workflow_delegations
               SET status=?, exit_code=?, finished_at=?
             WHERE execution_id=?
        """, (status, data.exit_code, timestamp, execution_id))
        phase = store.conn.execute(
            "SELECT provider,agent_id,session_id FROM workflow_phases WHERE plan_id=? AND id=?",
            (execution["plan_id"], execution["phase_id"]),
        ).fetchone()
        # A late completion cannot override an explicitly reassigned phase.
        # With no session ID, a different agent/provider must still be protected.
        still_owner = (
            phase is not None
            and phase["provider"] == execution["provider"]
            and phase["agent_id"] == execution["agent_id"]
            and phase["session_id"] == execution["session_id"]
        )
        if still_owner:
            update_phase(
                store, execution["plan_id"], execution["phase_id"],
                PhaseUpdate(status=status,
                            event_id="delegation:finish:" + execution_id),
            )
    return {
        "execution_id": execution_id,
        "duplicate": False,
        "status": status,
        "phase_updated": still_owner,
    }


def list_for_plan(store, plan_id: str) -> list[dict]:
    initialize(store)
    with store.lock:
        return [dict(row) for row in store.conn.execute("""
            SELECT * FROM workflow_delegations
            WHERE plan_id=? ORDER BY started_at,execution_id
        """, (plan_id,))]
