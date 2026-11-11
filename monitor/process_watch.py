"""Local process-to-session correlation for Claude Code and Codex.

A PID is a live process, not necessarily a session. A session is confirmed
only when its exact UUID is an argument of the identified CLI process.
Never use project name or raw substring matching as proof of identity.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import psutil

UUID_RE = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
    re.I,
)
CLAUDE_HINTS = ("claude", "@anthropic-ai/claude-code", "claude-code")
CODEX_HINTS = ("codex", "@openai/codex")


def _provider(name: str, cmdline: list[str]) -> str | None:
    """Identify the binary/entrypoint only, never search user prompt text."""
    binary = Path(name or "").stem.lower()
    if binary in ("claude", "claude-code"):
        return "claude"
    if binary in ("codex", "codex-cli"):
        return "codex"
    # Node/npm wrappers: inspect only the launch script and first command,
    # not arbitrary prompt arguments.
    launch = [Path(str(arg)).name.lower() for arg in cmdline[:3]]
    full_launch = [str(arg).lower().replace("\\", "/") for arg in cmdline[:3]]
    if binary in ("node", "node.exe", "bun", "bun.exe", "npm", "npx", "cmd", "powershell", "pwsh"):
        if any("anthropic-ai/claude-code" in x for x in full_launch) or "claude" in launch[1:]:
            return "claude"
        if any("@openai/codex" in x for x in full_launch) or "codex" in launch[1:]:
            return "codex"
    return None


def _resume_id(provider: str, argv: list[str]) -> str | None:
    """Only explicit --resume <uuid>, --resume=<uuid>, or resume <uuid>."""
    for idx, value in enumerate(argv):
        arg = str(value).lower()
        if arg == "--resume" and idx + 1 < len(argv):
            candidate = str(argv[idx + 1])
        elif arg.startswith("--resume="):
            candidate = arg[len("--resume="):]
        elif arg == "resume" and idx + 1 < len(argv):
            candidate = str(argv[idx + 1])
        else:
            continue
        if UUID_RE.fullmatch(candidate):
            return candidate.lower()
    return None


def _session_uuid(provider: str, source_path: str) -> str | None:
    path = Path(source_path)
    if provider == "claude":
        # Claude main: <uuid>.jsonl; subagents use parent <uuid> directory.
        if UUID_RE.fullmatch(path.stem):
            return path.stem.lower()
        for part in reversed(path.parts[:-1]):
            if UUID_RE.fullmatch(part):
                return part.lower()
    else:
        # Codex: rollout-YYYY-MM-DDTHH-MM-SS-<uuid>.jsonl
        match = UUID_RE.search(path.stem)
        if match:
            return match.group(1).lower()
    return None


class ProcessRegistry:
    """Time-limited process snapshot with privacy-preserving public fields."""

    def __init__(self, ttl_seconds: float = 5.0):
        self.ttl = ttl_seconds
        self._lock = Lock()
        self._updated = 0.0
        self._processes: list[dict] = []
        self._errors: list[str] = []

    def snapshot(self) -> dict:
        with self._lock:
            if time.monotonic() - self._updated >= self.ttl:
                self._refresh()
            return {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "processes": [dict(p) for p in self._processes],
                "errors": list(self._errors),
            }

    def _refresh(self):
        processes = []
        errors = []
        for process in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                with process.oneshot():
                    info = process.info
                    cmdline = info.get("cmdline") or []
                    provider = _provider(info.get("name") or "", cmdline)
                    if not provider:
                        continue
                    session_id = _resume_id(provider, cmdline)
                    processes.append({
                        "pid": info["pid"],
                        "provider": provider,
                        "session_id": session_id,
                        "started_at": datetime.fromtimestamp(
                            info["create_time"], timezone.utc
                        ).isoformat() if info.get("create_time") else None,
                    })
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except Exception as exc:
                errors.append(type(exc).__name__)
        self._processes = processes
        self._errors = errors[:5]
        self._updated = time.monotonic()

    def classify(self, runs: list[dict], observer_files: list[dict]) -> dict:
        snap = self.snapshot()
        by_run: dict[str, tuple[str, str]] = {}
        file_activity: dict[str, float] = {}
        for entry in observer_files:
            source_path = entry["path"]
            uuid = _session_uuid(entry["provider"], source_path)
            if uuid:
                by_run[entry["run_id"]] = (entry["provider"], uuid)
            try:
                last_modified = Path(source_path).stat().st_mtime
                rid = entry["run_id"]
                file_activity[rid] = max(file_activity.get(rid, 0), last_modified)
            except OSError:
                pass
        assigned = set()
        for run in runs:
            key = by_run.get(run["id"])
            linked = [p for p in snap["processes"]
                      if key and (p["provider"], p["session_id"]) == key]
            # Only the actual transcript file mtime implies agent activity.
            # DB updated_at can be advanced by an observer-generated idle event.
            recent = time.time() - file_activity.get(run["id"], 0) < 120
            if linked:
                assigned.update(p["pid"] for p in linked)
                presence = "active" if recent else "waiting"
                correlation = "verified_resume_id"
            elif run["status"] == "running" and recent:
                presence = "recent_unverified"
                correlation = "none"
            else:
                presence = "inactive"
                correlation = "none"
            # A durable terminal state takes precedence over a recent file
            # update, unless a process is proven to belong to that session.
            if not linked and run["status"] in ("completed", "failed", "cancelled"):
                presence = "inactive"
            run["presence"] = presence
            run["process_verified"] = bool(linked)
            run["correlation"] = correlation
            run["pids"] = [p["pid"] for p in linked]
        snap["unmatched_processes"] = [
            p for p in snap["processes"] if p["pid"] not in assigned
        ]
        snap["verified_sessions"] = sum(r["process_verified"] for r in runs)
        return snap
