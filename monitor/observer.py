"""Local read-only JSONL observer for Claude Code and Codex CLI.

Only metadata is persisted: no prompts, tool arguments, outputs, or transcript content.
The observer does not modify either provider's session files or configuration.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db import Store

log = logging.getLogger(__name__)
NAMESPACE = uuid.UUID("269c06e4-5530-47c8-a064-a02e738ce48e")
FINISHED = {"completed", "failed", "cancelled"}
MAX_RECORD_BYTES = 8 * 1024 * 1024  # skip individual oversized transcript lines
MAX_TOOLS_PER_SESSION = 100


def stable(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, ":".join(parts)))


def positive_int(value):
    return value if isinstance(value, int) and value >= 0 else 0


def path_roots():
    home = Path.home()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "projects"
    codex = Path(os.environ.get("CODEX_HOME", home / ".codex")) / "sessions"
    return {"claude": claude, "codex": codex}


def short_title(value, fallback="", limit=105):
    """Store only a short one-line task label, never an entire conversation."""
    if not isinstance(value,str):
        return fallback
    import re
    line = re.sub(r"\s+", " ", value).strip()
    if not line or line.startswith(("<", "{")):
        return fallback
    line = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|Bearer\s+\S+)", "[redacted]", line)
    return (line[:limit-1].rstrip()+"…") if len(line)>limit else line


def user_title(message):
    if isinstance(message,str): return short_title(message)
    if isinstance(message,dict):
        content=message.get("content")
        if isinstance(content,str): return short_title(content)
        if isinstance(content,list):
            for block in content:
                if isinstance(block,dict) and block.get("type")=="text":
                    title=short_title(block.get("text"))
                    if title:return title
    return ""


class SessionObserver:
    def __init__(self, store: Store, on_event=None, poll_seconds=2.0, max_age_hours=12):
        self.store = store
        self.on_event = on_event
        self.poll_seconds = poll_seconds
        self.max_age_seconds = max_age_hours * 3600
        self.stop_event = threading.Event()
        self.thread = None
        self.last_errors = {}
        with self.store.lock, self.store.conn:
            self.store.conn.executescript("""
                CREATE TABLE IF NOT EXISTS observer_files (
                    path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL, run_id TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observer_nodes (
                    run_id TEXT NOT NULL, identity TEXT NOT NULL,
                    node_id TEXT NOT NULL, PRIMARY KEY(run_id,identity)
                );
            """)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="agent-session-observer")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.scan_once()
            except Exception:
                log.exception("Session observer scan failed")
            self.stop_event.wait(self.poll_seconds)

    def _files(self):
        now = time.time()
        for provider, root in path_roots().items():
            if not root.exists():
                continue
            try:
                # Avoid recursive subdirectories outside expected provider paths.
                paths = root.rglob("*.jsonl")
                candidates = []
                for path in paths:
                    if not path.is_file():
                        continue
                    name = path.name
                    if provider == "codex" and not name.startswith("rollout-"):
                        continue
                    if provider == "claude" and ("agent-" in name or len(path.stem) >= 12):
                        pass
                    elif provider == "claude":
                        continue
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    if st.st_mtime >= now - self.max_age_seconds:
                        candidates.append((st.st_mtime, path))
                # Bound work even if years of transcript files exist.
                for _, path in sorted(candidates, reverse=True)[:250]:
                    yield provider, path
            except OSError:
                log.exception("Unable to inspect %s", root)

    def _offset(self, path):
        with self.store.lock:
            r = self.store.conn.execute("SELECT offset FROM observer_files WHERE path=?", (str(path),)).fetchone()
            return r["offset"] if r else 0

    def _save_offset(self, path, provider, run_id, offset):
        with self.store.lock, self.store.conn:
            self.store.conn.execute("""
                INSERT INTO observer_files(path,offset,provider,run_id,last_seen)
                VALUES(?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET offset=excluded.offset,
                    run_id=excluded.run_id,last_seen=excluded.last_seen
            """, (str(path), offset, provider, run_id, datetime.now(timezone.utc).isoformat()))

    def _emit(self, event):
        try:
            seq = self.store.save_event(event)
            if seq and self.on_event:
                self.on_event(event, seq)
        except ValueError as ex:
            log.warning("Observer event rejected: %s", ex)

    def _ensure_run(self, provider, session_id, cwd):
        rid = stable("run", provider, session_id)
        if self.store.snapshot(rid) is None:
            project = Path(cwd).name if cwd else "Unknown project"
            try:
                self.store.add_run(f"{provider.title()} · {project}", project, rid)
            except Exception:
                # Another observer may have inserted it in the meantime.
                if self.store.snapshot(rid) is None:
                    raise
        return rid

    def _ensure_node(self, rid, identity, title, agent, provider, parent=None, position=0):
        nid = stable("node", rid, identity)
        with self.store.lock:
            found = self.store.conn.execute("SELECT id FROM nodes WHERE id=?", (nid,)).fetchone()
        if not found:
            try:
                self.store.add_node(rid, title, parent_id=parent, agent=agent,
                                    provider=provider, position=position, node_id=nid)
            except Exception:
                with self.store.lock:
                    found = self.store.conn.execute("SELECT id FROM nodes WHERE id=?", (nid,)).fetchone()
                if not found:
                    raise
        return nid

    def _session_id(self, provider, path):
        if provider == "codex":
            # Codex filename contains a timestamp and UUID, but session_meta is canonical.
            return path.stem
        if path.name.startswith("agent-"):
            # Claude subagents under <session-id>/subagents/*.jsonl
            for part in reversed(path.parts[:-1]):
                try:
                    uuid.UUID(part)
                    return part
                except ValueError:
                    continue
        return path.stem

    def _scan_file(self, provider, path):
        session_id = self._session_id(provider, path)
        existing = self._offset(path)
        try:
            size = path.stat().st_size
            if existing > size:
                existing = 0  # log truncated/replaced
            with path.open("rb") as f:
                f.seek(existing)
                # Read complete lines only. Leave incomplete trailing JSON for next poll.
                # Limit per-file work per scan to avoid starving active sessions.
                for _ in range(150):
                    start = f.tell()
                    line = f.readline(MAX_RECORD_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_RECORD_BYTES:
                        # Too large: avoid repeatedly rereading it.
                        if line.endswith(b"\n"):
                            self._save_offset(path, provider, stable("run",provider,session_id), f.tell())
                        break
                    if not line.endswith(b"\n"):
                        break
                    try:
                        item = json.loads(line)
                        self._process(provider, path, session_id, item)
                    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, KeyError, ValueError):
                        log.debug("Skipped malformed %s record at %s", provider, start)
                    self._save_offset(path, provider, stable("run",provider,session_id), f.tell())
        except (PermissionError, FileNotFoundError, OSError) as ex:
            self.last_errors[str(path)] = str(ex)

    def scan_once(self):
        processed = 0
        recent: dict[str, float] = {}
        for provider, path in self._files():
            self._scan_file(provider, path)
            rid = stable("run", provider, self._session_id(provider, path))
            try:
                recent[rid] = max(recent.get(rid, 0), path.stat().st_mtime)
            except OSError:
                pass
            processed += 1
        # Idle means no new transcript activity; it does not imply completion.
        for rid, modified in recent.items():
            if time.time() - modified < 120:
                continue
            snap = self.store.snapshot(rid)
            if snap and snap["run"]["status"] == "running":
                self._emit({"run_id":rid,"type":"run.status","status":"idle",
                            "external_id":f"observer-idle:{rid}:{int(modified)}"})
        return processed

    def _process(self, provider, path, session_id, row):
        if not isinstance(row, dict):
            return
        if provider == "claude":
            self._claude(path, session_id, row)
        else:
            self._codex(path, session_id, row)

    def _claude(self, path, session_id, row):
        typ = row.get("type")
        cwd = row.get("cwd") or ""
        rid = self._ensure_run("claude", session_id, cwd)
        parent = self._ensure_node(rid, "session", "Claude Code session", "Claude", "claude")
        is_subagent = path.name.startswith("agent-")
        agent_id = row.get("agentId") or path.stem if is_subagent else None
        target = parent
        if is_subagent:
            target = self._ensure_node(rid, f"agent:{agent_id}", f"Subagent · {str(agent_id)[:12]}",
                                       f"Claude · {str(agent_id)[:12]}", "claude", parent)
            agent_task = short_title(row.get("description") or row.get("task"))
            if agent_task:
                self.store.set_node_title(target, agent_task, "Subagent ·")
        if typ == "user" and not is_subagent:
            title=user_title(row.get("message"))
            if title:
                self.store.set_run_title(rid, title, "Claude ·")
                self.store.set_node_title(parent, title, "Claude Code session")
        if typ == "assistant":
            msg = row.get("message") or {}
            usage = msg.get("usage") or {}
            msgid = msg.get("id")
            if msgid and usage:
                self._emit({"run_id": rid, "type": "usage", "node_id": target,
                            "agent": "Claude", "provider": "claude",
                            "usage": {
                                "session_id": f"claude:{session_id}:{agent_id or 'main'}",
                                "usage_key": f"message:{msgid}",
                                "model": msg.get("model") or "",
                                "input_tokens": positive_int(usage.get("input_tokens")),
                                "output_tokens": positive_int(usage.get("output_tokens")),
                                "cached_input_tokens": positive_int(usage.get("cache_read_input_tokens")),
                                "cost_source": "unavailable"
                            }, "external_id": f"claude-usage:{session_id}:{agent_id or 'main'}:{msgid}:{row.get('uuid')}"})
            for block in msg.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name") or "Tool"
                call = block.get("id")
                if not call:
                    continue
                # Do not persist tool input, prompts, or outputs.
                with self.store.lock:
                    count = self.store.conn.execute("SELECT COUNT(*) FROM nodes WHERE run_id=? AND parent_id=?",
                                                   (rid,target)).fetchone()[0]
                if count >= MAX_TOOLS_PER_SESSION:
                    continue
                tool_input=block.get("input") or {}
                if not isinstance(tool_input,dict): tool_input={}
                description=short_title(tool_input.get("description") or tool_input.get("task"))
                # Human-readable labels from Claude's Task/Agent tool description,
                # never the full delegated prompt.
                if name in ("Task","Agent") and description:
                    tool_title=f"Delegar · {description}"
                else:
                    tool_title=f"Tool · {name[:70]}"
                node = self._ensure_node(rid, f"tool:{agent_id or 'main'}:{call}",
                                         tool_title, "Claude", "claude", target)

                self._emit({"run_id":rid,"type":"node.status","node_id":node,
                            "status":"running","external_id":f"claude-tool-start:{call}"})
        elif typ == "user":
            msg = row.get("message") or {}
            for block in msg.get("content") if isinstance(msg.get("content"),list) else []:
                if not isinstance(block,dict) or block.get("type")!="tool_result":
                    continue
                call=block.get("tool_use_id")
                if not call: continue
                node=stable("node",rid,f"tool:{agent_id or 'main'}:{call}")
                with self.store.lock:
                    found=self.store.conn.execute("SELECT 1 FROM nodes WHERE id=?",(node,)).fetchone()
                if found:
                    self._emit({"run_id":rid,"type":"node.status","node_id":node,
                                "status":"failed" if block.get("is_error") else "completed",
                                "external_id":f"claude-tool-stop:{call}"})
        if typ in ("assistant","user"):
            self._emit({"run_id":rid,"type":"run.status","status":"running",
                        "external_id":f"claude-active:{session_id}:{row.get('uuid') or row.get('timestamp')}"})

    def _codex(self, path, session_id, row):
        typ = row.get("type")
        payload = row.get("payload") or {}
        if not isinstance(payload,dict): return
        canonical = session_id
        cwd = payload.get("cwd","") if typ=="session_meta" else ""
        rid = self._ensure_run("codex",canonical,cwd)
        root = self._ensure_node(rid,"session","Codex CLI session","Codex","codex")
        if typ == "event_msg":
            ev = payload.get("type")
            if ev == "user_message":
                title=short_title(payload.get("message"))
                if title:
                    self.store.set_run_title(rid,title,"Codex ·")
                    self.store.set_node_title(root,title,"Codex CLI session")
            if ev == "token_count":
                info=payload.get("info") or {}
                totals=info.get("total_token_usage") or {}
                if totals:
                    # Cumulative snapshot, never sum the successive token_count events.
                    self._emit({"run_id":rid,"type":"usage","node_id":root,
                                "provider":"codex","agent":"Codex",
                                "usage":{"session_id":f"codex:{canonical}",
                                         "usage_key":"session:cumulative",
                                         "input_tokens":positive_int(totals.get("input_tokens")),
                                         "output_tokens":positive_int(totals.get("output_tokens")),
                                         "cached_input_tokens":positive_int(totals.get("cached_input_tokens")),
                                         "reasoning_tokens":positive_int(totals.get("reasoning_output_tokens")),
                                         "cost_source":"unavailable"},
                                "external_id":f"codex-tokens:{canonical}:{row.get('timestamp')}:{json.dumps(totals,sort_keys=True)}"})
            elif ev in ("task_started","task_complete","turn_aborted"):
                status={"task_started":"running","task_complete":"completed","turn_aborted":"failed"}[ev]
                self._emit({"run_id":rid,"type":"node.status","node_id":root,
                            "status":status,"external_id":f"codex-turn:{ev}:{row.get('timestamp')}"})
                self._emit({"run_id":rid,"type":"run.status","status":status,
                            "external_id":f"codex-run:{ev}:{row.get('timestamp')}"})
        elif typ == "response_item":
            kind=payload.get("type")
            if kind == "message" and payload.get("role") == "user":
                title=user_title(payload)
                if title:
                    self.store.set_run_title(rid,title,"Codex ·")
                    self.store.set_node_title(root,title,"Codex CLI session")
            if kind in ("function_call","custom_tool_call"):
                call=payload.get("call_id")
                if call:
                    name=payload.get("name") or kind
                    with self.store.lock:
                        count=self.store.conn.execute("SELECT COUNT(*) FROM nodes WHERE run_id=? AND parent_id=?",
                                                      (rid,root)).fetchone()[0]
                    if count >= MAX_TOOLS_PER_SESSION:
                        return
                    nid=self._ensure_node(rid,f"tool:{call}",f"Tool · {name[:70]}",
                                          "Codex","codex",root)
                    self._emit({"run_id":rid,"type":"node.status","node_id":nid,
                                "status":"running","external_id":f"codex-tool-start:{canonical}:{call}"})
            elif kind in ("function_call_output","custom_tool_call_output"):
                call=payload.get("call_id")
                if call:
                    nid=stable("node",rid,f"tool:{call}")
                    with self.store.lock:
                        found=self.store.conn.execute("SELECT 1 FROM nodes WHERE id=?",(nid,)).fetchone()
                    if found:
                        self._emit({"run_id":rid,"type":"node.status","node_id":nid,
                                    "status":"completed","external_id":f"codex-tool-stop:{canonical}:{call}"})
