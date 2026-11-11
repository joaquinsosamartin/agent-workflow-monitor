"""Local Claude Mod ingestion with deterministic sessions and idempotent updates.

Only explicit metadata is accepted. No prompts, tool arguments or tool outputs.
"""
from __future__ import annotations
import ntpath
from pydantic import BaseModel, Field
from typing import Literal

from .db import Store
from .observer import stable



class ModAgent(BaseModel):
    id: str = Field(min_length=1,max_length=128)
    title: str = Field(min_length=1,max_length=150)
    status: Literal["pending","running","waiting","idle","completed","failed","killed"] = "pending"
    model: str = Field(default="",max_length=100)


class ModEvent(BaseModel):
    session_id: str = Field(min_length=1,max_length=128)
    cwd: str = Field(default="",max_length=1024)
    status: Literal["running","waiting","idle","completed","failed"] = "running"
    agents: list[ModAgent] = Field(default_factory=list,max_length=50)
    tool: str | None = Field(default=None,max_length=100)
    agent_id: str | None = Field(default=None,max_length=128)
    event_id: str = Field(min_length=1,max_length=128)
    # Context occupancy is not the billable usage and is intentionally
    # excluded from the token_usage table.
    context_tokens: int | None = Field(default=None,ge=0)
    context_window: int | None = Field(default=None,ge=0)
    hook_latency_ms: float | None = Field(default=None, ge=0, le=60000)


def ingest(store: Store, event: ModEvent) -> dict:
    run_id = stable("run", "claude", event.session_id)
    project = ntpath.basename(event.cwd.rstrip("/\\ ")) if event.cwd else "Claude Code"
    if not project:
        project = "Claude Code"

    with store.lock:
        row = store.conn.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            store.add_run(f"Claude Mod · {project}", project, run_id)
        else:
            with store.conn:
                store.conn.execute(
                    "UPDATE runs SET project=? WHERE id=? AND project IN ('Claude Code','Unknown project')",
                    (project, run_id)
                )

    session_node = stable("node", run_id, "session")
    with store.lock:
        found = store.conn.execute("SELECT id FROM nodes WHERE id=?", (session_node,)).fetchone()
        if not found:
            store.add_node(run_id, "Claude Code · Main session", agent="Claude",
                           provider="claude", node_id=session_node)

    # Store one stable child per subagent; never allocate a new node per poll.
    for index, agent in enumerate(event.agents):
        node_id = stable("node", run_id, f"agent:{agent.id}")
        with store.lock:
            row = store.conn.execute("SELECT id,title FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                store.add_node(run_id,agent.title,parent_id=session_node,
                               agent=f"Claude · {agent.id[:18]}",provider="claude",
                               position=index,node_id=node_id)
            elif row["title"] != agent.title:
                store.set_node_title(node_id,agent.title)
            current = store.conn.execute(
                "SELECT status FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
        normalized = {"idle":"idle","killed":"cancelled"}.get(agent.status,agent.status)
        if current is None or current["status"] != normalized:
            store.save_event({
                "run_id":run_id,"type":"node.status","node_id":node_id,
                "status":normalized,"agent":f"Claude · {agent.id[:18]}",
                "provider":"claude",
                "external_id":f"mod:{event.session_id}:{event.event_id}:agent:{agent.id}"
            })
    if event.tool:
        # Keep only the name; never include the call arguments/output.
        store.save_event({
            "run_id":run_id,"type":"log","node_id":session_node,
            "message":f"{(event.agent_id or 'Claude')[:40]} · {event.tool}",
            "external_id":f"mod:{event.session_id}:{event.event_id}:tool"
        })
    with store.lock:
        current_run = store.conn.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    if current_run and current_run["status"] != event.status:
        store.save_event({
            "run_id":run_id,"type":"run.status","status":event.status,
            "external_id":f"mod:{event.session_id}:{event.event_id}:status"
        })
    return {"run_id":run_id,"session_node_id":session_node,"agents":len(event.agents)}
