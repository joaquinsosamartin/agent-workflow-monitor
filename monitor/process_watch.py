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
from threading import Lock, Thread

import psutil

UUID_RE = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
    re.I,
)
CLAUDE_HINTS = ("claude", "@anthropic-ai/claude-code", "claude-code")
CODEX_HINTS = ("codex", "@openai/codex")


def _provider(name: str, cmdline: list[str]) -> str | None:
    """Identify the binary/entrypoint only, never search user prompt text."""
    # pathlib.Path on Linux does not recognize Windows backslashes in paths;
    # normalize both Win32 and POSIX executable naming conventions.
    binary = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if binary.endswith(".exe"): binary = binary[:-4]
    if binary in ("claude", "claude-code"):
        return "claude"
    if binary in ("codex", "codex-cli"):
        return "codex"
    # Node/npm wrappers: inspect only the launch script and first command,
    # not arbitrary prompt arguments.
    launch = [str(arg).replace("\\", "/").rsplit("/", 1)[-1].lower() for arg in cmdline[:4]]
    full_launch = [str(arg).lower().replace("\\", "/") for arg in cmdline[:4]]
    if binary in ("node", "node.exe", "bun", "bun.exe", "npm", "npx", "cmd", "powershell", "pwsh"):
        if any("anthropic-ai/claude-code" in x for x in full_launch) or any(x in ("claude", "claude.exe", "claude.cmd") for x in launch[1:]):
            return "claude"
        if any("@openai/codex" in x for x in full_launch) or any(x in ("codex", "codex.exe", "codex.cmd") for x in launch[1:]):
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
        self._refreshing = False

    def snapshot(self) -> dict:
        """Never block HTTP requests on Windows process enumeration."""
        with self._lock:
            stale = time.monotonic() - self._updated >= self.ttl
            if stale and not self._refreshing:
                self._refreshing = True
                Thread(target=self._refresh_background, daemon=True,
                       name="agent-process-sampler").start()
            return {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "processes": [dict(p) for p in self._processes],
                "errors": list(self._errors),
                "refreshing": self._refreshing,
            }

    def _refresh_background(self):
        try:
            processes, errors = self._collect()
        except Exception as exc:
            processes = None
            errors = [type(exc).__name__]
        with self._lock:
            if processes is not None:
                self._processes = processes
            self._errors = errors[:5]
            self._updated = time.monotonic()
            self._refreshing = False

    def _collect(self):
        processes = []
        errors = []
        for process in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
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
                        "ppid": info.get("ppid"),
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
        return processes, errors

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
        snap["processes_by_provider"] = {
            name: sum(p["provider"] == name for p in snap["processes"])
            for name in ("codex", "claude")
        }
        # Process ancestry can show that Codex launched Claude through an
        # intermediary shell. It is NOT proof of task/session delegation.
        by_pid = {p["pid"]: p for p in snap["processes"]}
        relations = []
        for child in snap["processes"]:
            parent_pid = child.get("ppid")
            visited = {child["pid"]}
            for depth in range(1, 9):
                if not parent_pid or parent_pid in visited:
                    break
                visited.add(parent_pid)
                parent = by_pid.get(parent_pid)
                if parent:
                    relations.append({
                        "parent_pid":parent_pid,"child_pid":child["pid"],
                        "parent_provider":parent["provider"],
                        "child_provider":child["provider"],
                        "relation":"process_parent" if depth == 1 else "process_ancestor",
                        "depth":depth
                    })
                    break
                try:
                    parent_pid = psutil.Process(parent_pid).ppid()
                except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                    break
        snap["process_relationships"] = relations
        return snap
